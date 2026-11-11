# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Admin tier: database upkeep and configuration.

These go through the ``hs`` command line in a child process. That is the interface HyperShell
supports for them, it keeps their prompts and printing away from this server's own streams, and it
means each one behaves exactly as it does for an operator at a shell.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Annotated, Any, Callable, Literal

from fastmcp import FastMCP
from pydantic import Field

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import BackendError, lib, memory_text
from hypershell_mcp.models import AdminResult
from hypershell_mcp.tools import tool

__all__ = ['register']

SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,100}')

def whole(low: int = 0, high: int = 10 ** 9) -> Callable[[Any], str]:
    """A setting that is a whole number between `low` and `high`."""
    def check(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError('a whole number')
        if not low <= int(value) <= high:
            raise ValueError(f'between {low} and {high}')
        return str(int(value))
    return check


def number(low: float, high: float) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f'a number between {low} and {high}')
        return str(value)
    return check


def one_of(*names: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if str(value).lower() not in names:
            raise ValueError('one of ' + ', '.join(names))
        return str(value).lower()
    return check


def truth(value: Any) -> str:
    if not isinstance(value, bool):
        raise ValueError('true or false')
    return 'true' if value else 'false'


def memory(value: Any) -> str:
    """Bytes as a whole number, or a size such as '4GB'."""
    if isinstance(value, str):
        return str(lib().parse_bytes(memory_text(value)))
    return whole()(value)


SETTABLE: dict[str, Callable[[Any], str]] = {
    'task.timeout': whole(), 'task.signalwait': whole(0, 3600), 'task.cores': whole(0, 4096), 'task.memory': memory,
    'submit.bundlesize': whole(1, 100_000), 'submit.bundlewait': whole(0, 3600),
    'server.attempts': whole(1, 100), 'server.eager': truth, 'server.poll': whole(1, 3600),
    'server.evict': whole(1, 86_400), 'server.bundlesize': whole(1, 100_000),
    'server.bundlemax': whole(1, 1_000_000), 'server.queuesize': whole(1, 100_000),
    'client.bundlesize': whole(1, 100_000), 'client.bundlewait': whole(0, 3600),
    'client.heartrate': whole(1, 3600), 'client.timeout': whole(), 'client.cores': whole(0, 4096),
    'client.memory': memory, 'client.ratelimit': whole(),
    'autoscale.policy': one_of('fixed', 'dynamic'), 'autoscale.factor': number(0.01, 1000),
    'autoscale.period': whole(1, 86_400), 'autoscale.size.init': whole(0, 100_000),
    'autoscale.size.min': whole(0, 100_000), 'autoscale.size.max': whole(1, 100_000),
    'logging.level': one_of('trace', 'debug', 'info', 'warning', 'error', 'critical'),
}
"""
The configuration values this server will write, each with the check its value must pass: how
tasks are scheduled, bundled, limited and retried. Everything else decides *what runs or where
data lives* (the database, exported environment, launchers, SSH, keys, working directory, and any
``_eval`` or ``_env`` key, which HyperShell resolves by running a command or reading the
environment). Those would let the admin tier execute commands, so they are left to someone editing
the file. Values are checked because HyperShell reads its configuration untyped: a log level it
does not know stops every ``hs`` process on the site from starting, this server included.
"""


def register(mcp: FastMCP, app: App) -> None:
    backend = app.backend

    async def quiet(action: str) -> None:
        """Refuse an operation that rewrites the database while tasks are in flight."""
        counts = await backend.call(backend.count, None)
        if counts.running or await asyncio.to_thread(app.clusters.running):
            raise BackendError(f'Cannot {action} while tasks are running or a cluster is up: it rewrites '
                               'the database under a live scheduler. Stop the cluster (stop_cluster) and '
                               'let running tasks finish first.')

    async def initdb(action: str, *flags: str, path: str | None = None) -> AdminResult:
        status, output = await app.hs('initdb', *flags, '--yes', timeout=3600)
        backend.forget_database()
        detail = output or ('done' if status == 0 else f'hs initdb exited {status}')
        return AdminResult(action=action, ok=status == 0, detail=detail[-2000:], path=path if status == 0 else None)

    @tool(mcp, app, Tier.ADMIN, 'Initialize database', idempotent=True)
    async def init_database() -> AdminResult:
        """
        Create HyperShell's tables, or upgrade an existing database from an earlier release in
        place. SQLite and Turso databases are created and upgraded automatically on first use, so
        this is needed only for PostgreSQL. Safe to repeat: it never removes data.
        """
        status, output = await app.hs('initdb', '--yes', timeout=1800)
        backend.forget_database()
        return AdminResult(action='init', ok=status == 0, detail=(output or 'database is ready')[-2000:])

    @tool(mcp, app, Tier.ADMIN, 'Vacuum database', idempotent=True)
    async def vacuum_database() -> AdminResult:
        """
        Compact the database, reclaiming the space left by deleted and rotated tasks. Worth doing
        after deleting many tasks. Refused while tasks are running.
        """
        await quiet('vacuum the database')
        return await initdb('vacuum', '--vacuum')

    @tool(mcp, app, Tier.ADMIN, 'Back up database')
    async def backup_database(
            name: Annotated[str | None, Field(
                description='File name for the backup, without a directory. Default: a timestamp.')] = None,
    ) -> AdminResult:
        """
        Write a compacted copy of the whole database to a new file in this server's backup
        directory, and report its path. SQLite and Turso only. The live database is not changed,
        so this is safe at any time, including before a risky bulk change.
        """
        name = name or f"backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
        if not SAFE_NAME.fullmatch(name):
            raise BackendError('The backup name must be a plain file name: letters, digits and _ . + -')
        folder = app.state_dir / 'backups'
        target = folder / name
        if target.exists():
            raise BackendError(f'{target} already exists; choose another name')
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        return await initdb('backup', '--backup', str(target), path=str(target))

    @tool(mcp, app, Tier.ADMIN, 'Rotate database')
    async def rotate_database() -> AdminResult:
        """
        Move every completed task out of the main database into the next numbered partition file
        beside it, leaving the main database small and fast for what is still to run. SQLite and
        Turso only. Rotated tasks remain visible to the hs command line's listings, but this
        server's tools read the main database only, so they no longer appear here. Refused while
        tasks are running.
        """
        await quiet('rotate the database')
        return await initdb('rotate', '--rotate')

    @tool(mcp, app, Tier.ADMIN, 'Set configuration', idempotent=True)
    async def set_config(
            key: Annotated[str, Field(
                description='Dotted name of the value. Settable: ' + ', '.join(sorted(SETTABLE)) + '.')],
            value: Annotated[str | int | float | bool, Field(description='The new value.')],
            scope: Annotated[Literal['user', 'local', 'system'] | None, Field(
                description="Which configuration file to write: 'local' (this site's own file), 'user' "
                            "(this user's HyperShell config) or 'system'. Default: local when the site "
                            'has its own file, otherwise user.')] = None) -> AdminResult:
        """
        Write one scheduling or limit setting to a HyperShell configuration file: timeouts,
        bundle sizes, retries, polling, autoscaling sizes, log level. It applies to HyperShell
        processes started afterwards, such as the next cluster; this server and anything already
        running keep the values they loaded. Settings that decide what runs or where data lives
        (the database, exported environment, launchers, keys, this server's own mcp section) are
        not settable here. HyperShell keeps a dated copy of the file it replaces.
        """
        if key not in SETTABLE:
            raise BackendError(f'{key!r} is not settable through this server. Settable: '
                               f"{', '.join(sorted(SETTABLE))}. Other settings decide what runs or where "
                               'data lives; edit the configuration file to change them.')
        try:
            text = SETTABLE[key](value)
        except (ValueError, BackendError) as problem:
            raise BackendError(f'{value!r} is not a valid value for {key}: it must be {problem}') from None
        if scope is None:
            _, local = await app.hs('config', '--local', timeout=60)
            scope = 'local' if local.strip() else 'user'
        _, location = await app.hs('config', f'--{scope}', timeout=60)
        if not location.strip():
            raise BackendError(f'This site has no {scope} configuration file to write to.')
        # The scope comes first and the value is known not to start with '-', so hs cannot read
        # one as the other and write a different file from the one reported.
        status, output = await app.hs('config', 'set', f'--{scope}', key, text, timeout=60)
        return AdminResult(action='set_config', ok=status == 0,
                           detail=(output or f'{key} = {text} ({scope})')[-2000:],
                           path=location.strip() if status == 0 else None)
