# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Run tier: start and stop the clusters that execute tasks. This is where commands actually run."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import BackendError
from hypershell_mcp.clusters import ClusterOptions
from hypershell_mcp.models import ClusterInfo, RunResult, TaskResult, TaskSelector
from hypershell_mcp.tools import tool
from hypershell_mcp.tools.read import wait_until_done
from hypershell_mcp.tools.write import Commands, Tags, Template, submit

__all__ = ['register']

RESULTS_SHOWN = 50


def register(mcp: FastMCP, app: App) -> None:
    backend, limits = app.backend, app.settings.limits

    async def start(options: ClusterOptions, force: bool) -> ClusterInfo:
        """Start a cluster unless another server is already working this database."""
        if not force and not await asyncio.to_thread(app.clusters.running):
            queue = await backend.call(backend.queue)
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
        submitted = await submit(app, commands, template=template, tags=tags, group=None, zone=None, cores=None,
                                 memory=None, timeout=task_timeout, batch=None)
        selector = TaskSelector(batch=submitted.batch)
        running = await asyncio.to_thread(app.clusters.running)
        note = None
        if running:
            cluster = running[0]
            if cluster.zone:
                note = (f"The running cluster {cluster.id} serves zone '{cluster.zone}' only, so these tasks "
                        'wait until a cluster without a zone runs.')
        else:
            cluster = await start(ClusterOptions(workers=workers, mode='drain', cwd=cwd), force=False)

        started = time.monotonic()
        budget = float(min(timeout, limits.wait))
        waited = await wait_until_done(app, ctx, selector, budget, watch_cluster=False)
        while not waited.done and time.monotonic() - started < budget:
            # The cluster ended with our tasks unfinished: either it was already draining when we
            # submitted (start another), or it halted on something it could not get past.
            if await asyncio.to_thread(app.clusters.running):
                break
            previous = await asyncio.to_thread(app.clusters.get, cluster.id, 15)
            if previous.exit_code not in (0, None):
                note = (f'Cluster {previous.id} stopped with exit status {previous.exit_code} before the tasks '
                        'finished. Its log ends:\n' + '\n'.join(previous.log_tail[-8:]))
                break
            cluster = await start(ClusterOptions(workers=workers, mode='drain', cwd=cwd), force=False)
            waited = await wait_until_done(app, ctx, selector, budget - (time.monotonic() - started),
                                           watch_cluster=False)

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
        if note is None:
            note = waited.note
        if submitted.submitted > RESULTS_SHOWN:
            extra = f'Showing {RESULTS_SHOWN} of {submitted.submitted} results; list_tasks with this batch has the rest.'
            note = f'{note} {extra}' if note else extra
        return RunResult(batch=submitted.batch, done=waited.done, counts=waited.counts,
                         waited_seconds=round(time.monotonic() - started, 1), cluster=cluster.id,
                         results=results, note=note)

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
        until stop_cluster. One cluster runs per site at a time. Task output is always captured,
        so get_task_output works for everything it runs. Follow progress with status or
        wait_for_tasks, and problems in list_clusters (its log).
        """
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
