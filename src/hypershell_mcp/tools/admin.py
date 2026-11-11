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
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import BackendError
from hypershell_mcp.models import AdminResult
from hypershell_mcp.tools import tool

__all__ = ['register']

SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]{0,100}$')
CONFIG_KEY = re.compile(r'^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)+$')


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
        if not SAFE_NAME.match(name):
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
                description="Dotted name of the value, e.g. 'task.timeout', 'server.attempts', "
                            "'client.heartrate'.")],
            value: Annotated[str | int | float | bool, Field(description='The new value.')],
            scope: Annotated[Literal['user', 'local', 'system'] | None, Field(
                description="Which configuration file to write: 'local' (this site's own file), 'user' "
                            "(this user's HyperShell config) or 'system'. Default: local when the site "
                            'has its own file, otherwise user.')] = None) -> AdminResult:
        """
        Write one value to a HyperShell configuration file. It applies to HyperShell processes
        started afterwards, such as the next cluster; this server and anything already running
        keep the values they loaded. This server's own settings (the mcp section) cannot be
        changed through it. HyperShell keeps a dated copy of the file it replaces.
        """
        if not CONFIG_KEY.match(key):
            raise BackendError(f"{key!r} is not a configuration name; write SECTION.NAME, e.g. 'task.timeout'")
        if key.split('.')[0] == 'mcp':
            raise BackendError("The mcp section holds this server's own security settings and is not "
                               'changeable through the server. Edit the configuration file directly.')
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if scope is None:
            _, local = await app.hs('config', '--local', timeout=60)
            scope = 'local' if local.strip() else 'user'
        _, location = await app.hs('config', f'--{scope}', timeout=60)
        if not location.strip():
            raise BackendError(f'This site has no {scope} configuration file to write to.')
        status, output = await app.hs('config', 'set', key, text, f'--{scope}', timeout=60)
        return AdminResult(action='set_config', ok=status == 0,
                           detail=(output or f'{key} = {text} ({scope})')[-2000:],
                           path=location.strip() if status == 0 else None)
