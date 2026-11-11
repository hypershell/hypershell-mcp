# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Run tier: start and stop the clusters that execute tasks. This is where commands actually run."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import BackendError
from hypershell_mcp.clusters import ClusterError, ClusterOptions
from hypershell_mcp.models import ClusterInfo, RunResult, StatusCounts, TaskResult, TaskSelector
from hypershell_mcp.tools import tool
from hypershell_mcp.tools.read import wait_until_done
from hypershell_mcp.tools.write import Commands, Tags, Template, submit

__all__ = ['register']

RESULTS_SHOWN = 50
DRAIN_WAIT = 20.0
"""Seconds an idle drain-mode cluster is given to exit before a new start is refused."""
RESTARTS = 3
"""Times run_tasks will start a cluster for one batch before it reports the problem instead."""


def register(mcp: FastMCP, app: App) -> None:
    backend, limits = app.backend, app.settings.limits

    async def start(options: ClusterOptions, force: bool) -> ClusterInfo:
        """
        Start a cluster, unless something is already working this database.

        A drain-mode cluster lingers for a few seconds after its last task. If one is in that state
        it is given time to exit (or to take up newly pending work, in which case it counts as
        busy), so a caller who retries tasks and restarts straight away is not turned back by a
        cluster that is on its way out.
        """
        deadline = time.monotonic() + DRAIN_WAIT
        while time.monotonic() < deadline:
            running = await asyncio.to_thread(app.clusters.running)
            if not running or running[0].mode != 'drain':
                break
            if (await backend.call(backend.count, None)).running:
                break
            await asyncio.sleep(0.5)
        if not force and not await asyncio.to_thread(app.clusters.running):
            queue = await app.queue()
            if queue.clients_connected:
                raise BackendError(
                    f'{queue.clients_connected} client(s) are connected to a HyperShell server that this MCP '
                    'server did not start. A second server on the same database would re-queue the tasks '
                    'the first is running. Newly submitted tasks will be picked up by that server; pass '
                    'force=true only if you know it is gone.')
        return await asyncio.to_thread(app.clusters.start, options)

    @tool(mcp, app, Tier.RUN, 'Run tasks now', open_world=True)
    async def run_tasks(
            ctx: Context,
            commands: Commands,
            template: Template = None,
            tags: Tags = None,
            workers: Annotated[int, Field(
                ge=1, le=1024, description='Tasks to run at once on this host, if a cluster has to be '
                                           'started.')] = 4,
            timeout: Annotated[int, Field(
                ge=1, description='Longest to wait for the tasks, in seconds (capped by the server). If '
                                  'it runs out, the tasks keep running and done=false.')] = 120,
            task_timeout: Annotated[int | None, Field(
                ge=1, description='Wall-time limit for each task, in seconds.')] = None,
            cwd: Annotated[str | None, Field(
                description='Directory to run the tasks in, if a cluster has to be started.')] = None,
            output_lines: Annotated[int, Field(
                ge=0, le=200, description='Lines of stdout and stderr to return per task.')] = 20) -> RunResult:
        """
        Submit commands and run them now, in parallel, then return each one's exit status and
        output. This is the quick path for a fan-out you want answered in this conversation: a
        parameter sweep, the same check over many files, a batch of conversions.

        The tasks go into the site's queue like any others. If no cluster is running, one is
        started on this host and exits when the queue is empty; if one is running, it picks the
        tasks up. A cluster works through the whole queue, so tasks submitted earlier and still
        pending run too. For long campaigns, or to run on other nodes, use submit_tasks and
        start_cluster and check back with wait_for_tasks.
        """
        if cwd is not None and not Path(cwd).expanduser().is_dir():
            raise BackendError(f'the working directory {cwd} does not exist; nothing was submitted')
        submitted = await submit(app, commands, template=template, tags=tags, group=None, zone=None, cores=None,
                                 memory=None, timeout=task_timeout, batch=None)
        selector = TaskSelector(batch=submitted.batch)
        started = time.monotonic()
        used: list[ClusterInfo] = list(await asyncio.to_thread(app.clusters.running))
        notes: list[str] = []
        if used and used[0].zone:
            notes.append(f"The running cluster {used[0].id} serves zone '{used[0].zone}' only, so these tasks "
                         'wait until a cluster without a zone runs.')
        starts = 0

        async def serve(counts: StatusCounts) -> str | None:
            """Nothing is running the batch: start a cluster, unless the last one failed."""
            nonlocal starts
            if used:
                previous = await asyncio.to_thread(app.clusters.get, used[-1].id, 15)
                if previous.exit_code not in (0, None):
                    return (f'Cluster {previous.id} stopped with exit status {previous.exit_code} before the '
                            'tasks finished. Its log ends:\n' + '\n'.join(previous.log_tail[-8:]))
            if starts >= RESTARTS:
                return (f'{counts.remaining} tasks are still unfinished after {starts} clusters started and '
                        'exited without running them. list_clusters shows their logs.')
            starts += 1
            try:
                used.append(await start(ClusterOptions(workers=workers, mode='drain', cwd=cwd), force=False))
            except (BackendError, ClusterError) as failure:
                # The tasks are already queued. Say so with the batch, rather than raise and leave
                # the caller not knowing that a second attempt would submit them twice.
                return f'The tasks are submitted but no cluster could be started for them: {failure}'
            return None

        waited = await wait_until_done(app, ctx, selector, float(min(timeout, limits.wait)), when_unserved=serve)
        if waited.note:
            notes.append(waited.note)

        _, rows = await backend.call(backend.select, selector, ['id', 'args', 'status', 'exit_status', 'duration'],
                                     'submit_time', False, RESULTS_SHOWN, 0)
        order = {task_id: index for index, task_id in enumerate(submitted.ids)}
        rows.sort(key=lambda row: order.get(row['id'], len(order)))
        results = []
        for row in rows:
            result = TaskResult(id=row['id'], command=row['args'], status=row['status'],
                                exit_status=row['exit_status'], duration=row['duration'])
            if output_lines and row['status'] in ('succeeded', 'failed'):
                out = await app.task_output(row['id'], 'stdout', lines=output_lines, max_bytes=4096)
                err = await app.task_output(row['id'], 'stderr', lines=output_lines, max_bytes=4096)
                result.stdout = out.text if out.available else None
                result.stderr = err.text if err.available else None
                result.truncated = out.truncated or err.truncated
            results.append(result)
        if submitted.submitted > RESULTS_SHOWN:
            notes.append(f'Showing {RESULTS_SHOWN} of {submitted.submitted} results; list_tasks with this batch '
                         'has the rest.')
        return RunResult(batch=submitted.batch, done=waited.done, counts=waited.counts,
                         waited_seconds=round(time.monotonic() - started, 1),
                         cluster=used[-1].id if used else None, results=results, note=' '.join(notes) or None)

    @tool(mcp, app, Tier.RUN, 'Start cluster', open_world=True)
    async def start_cluster(
            options: Annotated[ClusterOptions | None, Field(
                description='How to run it. Omit to run 4 tasks at a time on this host until the queue '
                            'is empty.')] = None,
            force: Annotated[bool, Field(
                description='Start even though clients are connected to another server on this '
                            'database.')] = False) -> ClusterInfo:
        """
        Start a HyperShell cluster to work through the queue: the scheduler plus the clients that
        run tasks, on this host or, with a launcher, on other nodes ('ssh' with hosts, 'mpi', or
        'custom' with a command such as 'srun --exclusive'). It runs detached: it keeps going
        after this call returns and after this session ends.

        mode 'drain' (default) exits when no tasks remain; 'forever' keeps serving new submissions
        until stop_cluster. One cluster runs per site at a time: called without options while one
        is already serving the queue, this returns that cluster. Task output is always captured,
        so get_task_output works for everything it runs. Follow progress with status or
        wait_for_tasks, and problems in list_clusters (its log).
        """
        if options is None:
            running = await asyncio.to_thread(app.clusters.running)
            if running and (running[0].mode == 'forever' or (await backend.call(backend.count, None)).running):
                return running[0]  # already serving the queue, which is all a bare call asks for
        return await start(options or ClusterOptions(), force)

    @tool(mcp, app, Tier.RUN, 'Stop cluster', destructive=True, idempotent=True)
    async def stop_cluster(
            cluster_id: Annotated[str | None, Field(
                description='The cluster to stop. Omit for the one that is running.')] = None,
            how: Annotated[Literal['drain', 'interrupt', 'kill'], Field(
                description="'drain': start no new tasks, let running ones finish, then exit. "
                            "'interrupt': also interrupt the running tasks. 'kill': terminate "
                            'everything now.')] = 'drain') -> ClusterInfo:
        """
        Stop a cluster this server started. 'drain' is graceful and loses no work, but returns
        before the cluster has exited: running tasks finish first. 'interrupt' and 'kill' stop
        running tasks, which are then recorded as failed or left marked running; the next cluster
        to start re-queues tasks left running. Pending tasks simply stay pending.
        """
        if cluster_id is None:
            running = await asyncio.to_thread(app.clusters.running)
            if not running:
                raise BackendError('No cluster started by this server is running.')
            cluster_id = running[0].id
        return await asyncio.to_thread(app.clusters.stop, cluster_id, how)
