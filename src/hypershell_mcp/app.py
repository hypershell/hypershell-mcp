# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
What every tool shares: the settings, the backend, the cluster manager, and who is calling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_access_token

from hypershell_mcp.auth import TIERS, Tier, scope_for
from hypershell_mcp.backend import Backend, BackendError, hostname, lib
from hypershell_mcp.clusters import ClusterManager, child_environment
from hypershell_mcp.models import AccessInfo, OutputChunk, QueueInfo
from hypershell_mcp.outputs import read_output
from hypershell_mcp.settings import Settings

__all__ = ['App', 'Caller']

log = logging.getLogger('hypershell_mcp')


@dataclass(frozen=True)
class Caller:
    """The identity behind the current request."""

    subject: str | None
    tiers: frozenset[Tier]
    expires: int | None = None

    def allows(self, tier: Tier) -> bool:
        return tier in self.tiers


@dataclass
class App:
    """The server's shared state. One per process; tools close over it."""

    settings: Settings
    backend: Backend = field(default_factory=Backend)
    _clusters: ClusterManager | None = None
    _roots: tuple[Path, ...] | None = None
    tool_tiers: dict[str, Tier] = field(default_factory=dict)

    # ---------------------------------------------------------------------------------------------
    # Places

    @property
    def state_dir(self) -> Path:
        """Where this server keeps its own files: under the site library, beside HyperShell's."""
        return Path(str(lib().default_path.lib)) / 'mcp'

    @property
    def clusters(self) -> ClusterManager:
        if self._clusters is None:
            self._clusters = ClusterManager(self.state_dir / 'clusters')
        return self._clusters

    @property
    def output_roots(self) -> tuple[Path, ...]:
        """Directories task output may be read from: the site's own, plus any in ``mcp.outputs``."""
        if self._roots is None:
            roots = [Path(str(lib().default_path.lib)) / 'task']
            extra = (lib().config.get('mcp') or {}).get('outputs') or []
            if isinstance(extra, str):
                extra = [extra]
            roots.extend(Path(str(item)).expanduser() for item in extra)
            self._roots = tuple(roots)
        return self._roots

    # ---------------------------------------------------------------------------------------------
    # Who is calling

    def caller(self) -> Caller:
        """
        The current caller and the tiers open to them.

        With a token, the tiers are those its scopes grant, less any this server has switched off.
        Without one (stdio, or HTTP started with --no-auth) the caller is the local user and gets
        every tier the server offers.
        """
        token = get_access_token()
        offered = self.settings.tiers
        if token is None:
            return Caller(subject=None, tiers=offered)
        granted = frozenset(tier for tier in TIERS if scope_for(tier) in (token.scopes or []))
        subject = token.subject or (token.claims or {}).get('sub') or token.client_id
        return Caller(subject=subject, tiers=granted & offered, expires=token.expires_at)

    def access(self, transport: str | None) -> AccessInfo:
        caller = self.caller()
        expires = (datetime.fromtimestamp(caller.expires).isoformat(sep=' ', timespec='seconds')
                   if caller.expires else None)
        return AccessInfo(subject=caller.subject, transport=transport or self.settings.transport,
                          tiers=[tier.value for tier in TIERS if tier in caller.tiers], token_expires=expires)

    # ---------------------------------------------------------------------------------------------
    # Things more than one tool does

    async def queue(self) -> QueueInfo:
        """The state of the queue, discounting clients left behind by this server's last cluster."""
        stale_before = await asyncio.to_thread(self.clusters.last_exit)
        return await self.backend.call(self.backend.queue, stale_before)

    async def task_output(self, task_id: str, stream: str, *, mode: str = 'tail', lines: int = 100,
                          pattern: str | None = None, max_bytes: int | None = None) -> OutputChunk:
        """Read part of a task's captured output (see `hypershell_mcp.outputs`)."""
        paths = await self.backend.call(self.backend.output_paths, task_id)
        return await asyncio.to_thread(
            read_output, paths['id'], stream, paths[stream], roots=self.output_roots,  # type: ignore[arg-type]
            status=paths['status'], client_host=paths['client_host'], local_host=hostname(),
            mode=mode, lines=lines, pattern=pattern,  # type: ignore[arg-type]
            max_bytes=min(max_bytes or self.settings.limits.output, self.settings.limits.output))

    async def hs(self, *arguments: str, timeout: float = 600) -> tuple[int, str]:
        """
        Run ``hs`` with `arguments` as a child process and return its exit status and output.

        Used for the operations HyperShell only offers through its command line, and for the ones
        that print: a child cannot write to this server's stdout, which over stdio is the protocol.
        """
        environment = child_environment()
        environment['HYPERSHELL_LOGGING_COLOR'] = 'false'
        environment['NO_COLOR'] = '1'

        def run() -> tuple[int, str]:
            try:
                done = subprocess.run([sys.executable, '-c', 'import sys; from hypershell import main; '
                                       'sys.exit(main(sys.argv[1:]))', *arguments],
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=timeout, env=environment)
            except subprocess.TimeoutExpired:
                raise BackendError(f"'hs {' '.join(arguments)}' did not finish within {timeout:.0f} s") from None
            return done.returncode, (done.stdout + done.stderr).strip()

        return await asyncio.to_thread(run)

    def audit(self, *, tool: str, tier: Tier, arguments: dict[str, Any], ok: bool, error: str | None,
              seconds: float) -> None:
        """Append one line to the audit log for a call that could change something."""
        caller = self.caller()
        text = json.dumps(arguments, default=str)
        entry = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'subject': caller.subject, 'tool': tool,
                 'tier': tier.value, 'ok': ok, 'seconds': round(seconds, 3),
                 'arguments': text if len(text) <= 2000 else text[:2000] + '…'}
        if error:
            entry['error'] = error[:500]
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.state_dir / 'audit.jsonl', 'a') as stream:
                stream.write(json.dumps(entry) + '\n')
        except OSError as failure:
            log.warning('Could not write the audit log: %s', failure)
