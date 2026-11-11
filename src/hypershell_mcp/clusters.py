# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Clusters this server starts.

A cluster is a real ``hs cluster`` process: the HyperShell server that schedules tasks from the
database plus the clients that run them, locally or through SSH, MPI or a launcher such as
``srun``. It runs detached from this server, so a campaign outlives the MCP session that began it.
Each cluster has a directory under the site's library holding its record, its log and, once it
ends, its exit status. Records are how a later server process finds clusters it did not start.

Only one cluster per site runs at a time. A HyperShell server re-queues tasks it finds scheduled
but unfinished when it starts, so a second server on the same database would take a first
server's running tasks and run them again.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from hypershell_mcp.models import ClusterInfo
from hypershell_mcp.outputs import tail_text

__all__ = ['ClusterOptions', 'ClusterManager', 'ClusterError', 'RUNNER']

RUNNER: Final[str] = (
    'import sys\n'
    'from hypershell import main\n'
    'status = main(sys.argv[2:])\n'
    "open(sys.argv[1], 'w').write(str(status))\n"
    'sys.exit(status)\n'
)
"""Runs ``hs`` in a child interpreter and leaves its exit status in a file for later readers."""

SECRET_PREFIX: Final[str] = 'HYPERSHELL_MCP_'
"""This server's own settings, which must not reach the environment of the tasks a cluster runs."""

KILL_GRACE: Final[float] = 10.0


class ClusterError(Exception):
    """A cluster could not be started, found or stopped; the message says why."""


class ClusterOptions(BaseModel):
    """How to run a cluster. The defaults run tasks on this host until the queue is empty."""

    model_config = ConfigDict(extra='forbid')

    workers: Annotated[int, Field(
        ge=0, le=4096,
        description='Tasks each client runs at once (0 = one per core). Default 4.')] = 4
    mode: Annotated[Literal['drain', 'forever'], Field(
        description="'drain' runs until no tasks remain, then exits. 'forever' stays up waiting "
                    'for new submissions until it is stopped.')] = 'drain'
    max_retries: Annotated[int, Field(
        ge=0, le=100,
        description='Times a failed task is retried automatically. Note that this applies to '
                    'every failed task in the database, including old ones.')] = 0
    eager: Annotated[bool, Field(description='Schedule retries before new tasks.')] = False
    task_timeout: Annotated[int | None, Field(
        ge=1, description='Wall-time limit in seconds for every task (a task\'s own limit still '
                          'applies when it is shorter).')] = None
    launcher: Annotated[Literal['local', 'ssh', 'mpi', 'custom'], Field(
        description="Where clients run. 'local': on this host. 'ssh': on the given hosts. 'mpi': "
                    "under mpirun. 'custom': under launcher_command, e.g. 'srun --exclusive'.")] = 'local'
    hosts: Annotated[list[str] | None, Field(
        description="SSH hosts to start a client on (launcher 'ssh').")] = None
    ssh_group: Annotated[str | None, Field(
        description="Name of a host list in the HyperShell config (ssh.nodelist), instead of hosts.")] = None
    launcher_command: Annotated[str | None, Field(
        description="Command that starts each client (launcher 'custom').")] = None
    autoscaling: Annotated[Literal['fixed', 'dynamic'] | None, Field(
        description="Scale the number of clients through the launcher: 'fixed' keeps init_size "
                    "clients; 'dynamic' grows and shrinks with the queue between min_size and "
                    "max_size. Needs launcher 'custom'.")] = None
    init_size: Annotated[int | None, Field(ge=0, description='Clients at start (autoscaling).')] = None
    min_size: Annotated[int | None, Field(ge=0, description='Fewest clients (autoscaling).')] = None
    max_size: Annotated[int | None, Field(ge=1, description='Most clients (autoscaling).')] = None
    zone: Annotated[str | None, Field(
        description='Zone the clients serve. Clients without a zone run only tasks without one.')] = None
    template: Annotated[str | None, Field(
        description="Client-side command template applied to each task's args, e.g. "
                    "'python process.py {}'. Default '{}' runs the args as they are.")] = None
    bundlesize: Annotated[int | None, Field(
        ge=1, description='Tasks handed to a client at a time. Raise it for very short tasks.')] = None
    client_cores: Annotated[int | None, Field(
        ge=1, description='Cores a client may commit to tasks that declare cores.')] = None
    client_memory: Annotated[str | None, Field(
        description="Memory a client may commit to tasks that declare memory, e.g. '64GB'.")] = None
    client_timeout: Annotated[int | None, Field(
        ge=1, description='Seconds an idle client waits for work before it shuts down.')] = None
    monitor: Annotated[bool, Field(
        description='Also record the cores and memory each task actually used.')] = False
    cwd: Annotated[str | None, Field(
        description='Directory the tasks run in (local clients). Default: where this server '
                    'was started.')] = None

    def arguments(self) -> list[str]:
        """The ``hs cluster`` arguments for these options."""
        argv = ['cluster', '--restart' if self.mode == 'drain' else '--forever', '--capture',
                '-N', str(self.workers), '-Q', '2']
        if self.max_retries:
            argv += ['-r', str(self.max_retries)]
            if self.eager:
                argv.append('--eager')
        if self.task_timeout:
            argv += ['-W', str(self.task_timeout)]
        if self.template:
            argv += ['-t', self.template]
        if self.bundlesize:
            argv += ['-b', str(self.bundlesize)]
        if self.client_cores:
            argv += ['-C', str(self.client_cores)]
        if self.client_memory:
            argv += ['-M', self.client_memory]
        if self.client_timeout:
            argv += ['-T', str(self.client_timeout)]
        if self.monitor:
            argv.append('--monitor')
        if self.zone:
            argv += ['--zone', self.zone]
        if self.launcher == 'ssh':
            if self.ssh_group:
                argv += ['--ssh-group', self.ssh_group]
            elif self.hosts:
                argv += ['--ssh', *self.hosts]
            else:
                raise ClusterError("launcher 'ssh' needs hosts or ssh_group")
        elif self.launcher == 'mpi':
            argv.append('--mpi')
        elif self.launcher == 'custom':
            if not self.launcher_command:
                raise ClusterError("launcher 'custom' needs launcher_command, e.g. 'srun --exclusive'")
            argv.append(f'--launcher={self.launcher_command}')
        if self.autoscaling:
            if self.launcher != 'custom':
                raise ClusterError("autoscaling needs launcher 'custom' with a launcher_command")
            argv += ['--autoscaling', self.autoscaling]
            for flag, value in (('-I', self.init_size), ('-X', self.min_size), ('-Y', self.max_size)):
                if value is not None:
                    argv += [flag, str(value)]
        return argv


class ClusterManager:
    """Starts, finds and stops the clusters recorded under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._children: dict[str, subprocess.Popen[bytes]] = {}

    # ---------------------------------------------------------------------------------------------

    def start(self, options: ClusterOptions) -> ClusterInfo:
        """Start a cluster, detached, and return its record."""
        running = self.running()
        if running:
            raise ClusterError(f'cluster {running[0].id} is already running (pid {running[0].pid}). One '
                               'cluster per site: it will pick up newly submitted tasks, or stop it first.')
        arguments = options.arguments()
        cwd = Path(options.cwd).expanduser() if options.cwd else Path.cwd()
        if not cwd.is_dir():
            raise ClusterError(f'the working directory {cwd} does not exist')
        cluster_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        folder = self.root / cluster_id
        folder.mkdir(parents=True, exist_ok=False)
        environment = {name: value for name, value in os.environ.items() if not name.startswith(SECRET_PREFIX)}
        environment.setdefault('HYPERSHELL_LOGGING_COLOR', 'false')
        with open(folder / 'cluster.log', 'ab') as log:
            process = subprocess.Popen(
                [sys.executable, '-c', RUNNER, str(folder / 'exit'), *arguments],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=environment,
                start_new_session=True, cwd=cwd)
        self._children[cluster_id] = process
        record = {'id': cluster_id, 'pid': process.pid, 'started': time.strftime('%Y-%m-%d %H:%M:%S'),
                  'mode': options.mode, 'workers': options.workers,
                  'launcher': None if options.launcher == 'local' else options.launcher,
                  'zone': options.zone, 'cwd': str(cwd), 'command': 'hs ' + ' '.join(quote(word) for word in arguments)}
        (folder / 'cluster.json').write_text(json.dumps(record, indent=2))
        time.sleep(0.4)  # long enough for a bad option to end the process
        info = self.get(cluster_id, log_lines=20)
        if info.state == 'exited' and info.exit_code not in (0, None):
            detail = '\n'.join(info.log_tail[-8:]) or 'no output'
            raise ClusterError(f'the cluster exited at once with status {info.exit_code}:\n{detail}')
        return info

    def get(self, cluster_id: str, log_lines: int = 0) -> ClusterInfo:
        folder = self.root / cluster_id
        if '/' in cluster_id or '..' in cluster_id or not (folder / 'cluster.json').is_file():
            raise ClusterError(f'no cluster {cluster_id!r}; list_clusters shows the ones this server started')
        record: dict[str, Any] = json.loads((folder / 'cluster.json').read_text())
        alive = self._alive(cluster_id, record['pid'], folder)
        exit_code = stopped = None
        if not alive:
            exit_file = folder / 'exit'
            if exit_file.is_file():
                try:
                    exit_code = int(exit_file.read_text().strip())
                except ValueError:
                    exit_code = None
                stopped = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exit_file.stat().st_mtime))
            else:
                child = self._children.get(cluster_id)
                exit_code = child.returncode if child is not None else None
                log = folder / 'cluster.log'
                if log.exists():
                    stopped = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(log.stat().st_mtime))
        return ClusterInfo(state='running' if alive else 'exited', exit_code=exit_code, stopped=stopped,
                           log_tail=tail_text(folder / 'cluster.log', log_lines) if log_lines else [],
                           **record)

    def list(self, log_lines: int = 0, limit: int = 20) -> list[ClusterInfo]:
        """Recorded clusters, newest first."""
        if not self.root.is_dir():
            return []
        names = sorted((entry.name for entry in self.root.iterdir() if (entry / 'cluster.json').is_file()),
                       reverse=True)
        return [self.get(name, log_lines=log_lines) for name in names[:limit]]

    def running(self) -> list[ClusterInfo]:
        return [info for info in self.list(limit=50) if info.state == 'running']

    def last_exit(self) -> float | None:
        """
        When the most recent cluster ended, as a timestamp; None if there is none or it is running.

        Whatever was connected to that cluster's server before then has no server now. A cluster
        that is killed cannot record its clients as disconnected or its tasks as stopped, so this
        is how the rest of the server knows to discount them.
        """
        latest = self.list(limit=1)
        if not latest or latest[0].state == 'running':
            return None
        folder = self.root / latest[0].id
        for name in ('exit', 'cluster.log', 'cluster.json'):
            if (folder / name).exists():
                return (folder / name).stat().st_mtime
        return None

    def log(self, cluster_id: str, lines: int) -> list[str]:
        self.get(cluster_id)
        return tail_text(self.root / cluster_id / 'cluster.log', lines, max_bytes=262_144)

    def stop(self, cluster_id: str, how: Literal['drain', 'interrupt', 'kill']) -> ClusterInfo:
        """
        Ask a cluster to stop.

        ``drain`` lets running tasks finish and starts no more. ``interrupt`` also interrupts the
        running tasks. ``kill`` terminates the process group at once; tasks that were running are
        left marked as running and are re-queued by the next cluster that starts.
        """
        info = self.get(cluster_id)
        if info.state != 'running':
            return info
        try:
            if how == 'drain':
                os.kill(info.pid, signal.SIGUSR1)
            elif how == 'interrupt':
                os.kill(info.pid, signal.SIGUSR2)
            else:
                os.killpg(info.pid, signal.SIGTERM)
                deadline = time.monotonic() + KILL_GRACE
                while time.monotonic() < deadline and self._alive(cluster_id, info.pid, self.root / cluster_id):
                    time.sleep(0.2)
                if self._alive(cluster_id, info.pid, self.root / cluster_id):
                    os.killpg(info.pid, signal.SIGKILL)
                    time.sleep(0.3)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise ClusterError(f'not allowed to signal pid {info.pid}: it belongs to another user') from None
        return self.get(cluster_id, log_lines=10)

    # ---------------------------------------------------------------------------------------------

    def _alive(self, cluster_id: str, pid: int, folder: Path) -> bool:
        """
        Whether the recorded process is still this cluster.

        A child of this server is asked directly, which also reaps it. A process left by an earlier
        server is matched on its command line, which carries the cluster's own directory: a pid
        that has since been reused by something else must never be reported, or signalled, as ours.
        """
        child = self._children.get(cluster_id)
        if child is not None:
            return child.poll() is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        try:
            command = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True,
                                     text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(folder / 'exit') in command


def quote(word: str) -> str:
    """Shell-quote a word for display."""
    import shlex
    return shlex.quote(word)
