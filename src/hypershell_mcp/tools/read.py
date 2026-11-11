# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Read tier: look at tasks, output, the queue and configuration. Nothing here changes anything."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Awaitable, Callable, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

import hypershell_mcp
from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import DEFAULT_FIELDS, GROUPABLE, hostname, lib
from hypershell_mcp.diagnose import build_report
from hypershell_mcp.models import (ClusterList, ConfigValue, FailureReport, OutputChunk, SiteStatus,
                                   StatusCounts, TagIndex, TaskDetail, TaskPage, TaskSelector,
                                   TaskSummary, WaitResult)
from hypershell_mcp.tools import tool

__all__ = ['register', 'wait_until_done', 'site_status']

LONG_TEXT = 400
"""Characters of a command shown in a listing; get_task shows it whole."""


async def site_status(app: App, transport: str | None) -> SiteStatus:
    """Everything `status` reports, also served as the ``hypershell://status`` resource."""
    backend = app.backend
    counts = await backend.call(backend.count, None)
    queue = await app.queue()
    running = await asyncio.to_thread(app.clusters.running)
    access = app.access(transport)

    hints: list[str] = []

    def tasks(number: int) -> str:
        return f'{number} task' if number == 1 else f'{number} tasks'
    serving = bool(running) or queue.clients_connected > 0
    if counts.total == 0:
        hints.append('The database has no tasks. submit_tasks adds some; run_tasks submits and runs them.')
    if counts.pending and not serving:
        hints.append(f'{tasks(counts.pending)} pending and nothing is running them. start_cluster runs '
                     'them here or through a launcher; otherwise they wait for a cluster started elsewhere.')
    if counts.running and not serving:
        hints.append(f'{tasks(counts.running)} marked running but no client is connected: probably '
                     'interrupted. The next cluster to start re-queues them.')
    if counts.failed:
        hints.append(f'{tasks(counts.failed)} failed. diagnose_failures groups them by cause.')
    if 'default' not in queue.zones_remaining and queue.zones_remaining and running and not running[0].zone:
        hints.append('The remaining tasks are all in named zones, which a cluster without a zone does not serve.')
    return SiteStatus(hypershell_version=lib().version, server_version=hypershell_mcp.__version__,
                      host=hostname(), database=backend.database_info(), tasks=counts, queue=queue,
                      clusters=running, access=access, hints=hints)


Unserved = Callable[[StatusCounts], Awaitable[str | None]]


async def wait_until_done(app: App, ctx: Context, selector: TaskSelector | None, timeout: float,
                          *, when_unserved: Unserved | None = None) -> WaitResult:
    """
    Poll until every matching task has finished, the time is up, or nothing could finish them.

    Progress is reported to the client as tasks complete. Tasks are "unserved" when some remain,
    none is running, and neither a cluster of this server's nor any connected client exists to run
    them. By default the wait then ends early and says so, since blocking would only hide the real
    state from the caller. A caller that can do something about it passes `when_unserved`, which
    is awaited with the current counts and returns None to keep waiting or a note to stop with.
    """
    backend = app.backend
    started = time.monotonic()
    grace = 3.0 if when_unserved is None else 0.0
    interval, note = 0.5, None
    while True:
        counts: StatusCounts = await backend.call(backend.count, selector)
        finished = counts.total - counts.remaining
        await ctx.report_progress(finished, counts.total or None,
                                  f'{finished}/{counts.total} finished, {counts.running} running')
        if counts.total == 0:
            note = 'No tasks match the selector.'
            break
        if counts.remaining == 0:
            break
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            note = f'Timed out after {timeout:.0f} s with {counts.remaining} tasks unfinished. Call again to keep waiting.'
            break
        if counts.running == 0 and elapsed >= grace and not await asyncio.to_thread(app.clusters.running):
            queue = await app.queue()
            if queue.clients_connected == 0:
                if when_unserved is None:
                    note = (f'{counts.pending} tasks are pending and nothing is running them: no cluster '
                            'started by this server is alive and no client is connected. Start one with '
                            'start_cluster.')
                    break
                note = await when_unserved(counts)
                if note is not None:
                    break
                interval = 0.5
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 5.0)
    return WaitResult(done=counts.done, counts=counts, waited_seconds=round(time.monotonic() - started, 1), note=note)


def register(mcp: FastMCP, app: App) -> None:
    backend, limits = app.backend, app.settings.limits

    @tool(mcp, app, Tier.READ, 'Site status')
    async def status(ctx: Context) -> SiteStatus:
        """
        One look at the whole site: task counts by state, what is left to run and in which groups
        and zones, how fast it is going, the clusters this server started, and what you are allowed
        to do. Start here, and come back here when unsure what to do next: `hints` says what looks
        worth doing.
        """
        return await site_status(app, ctx.transport)

    @tool(mcp, app, Tier.READ, 'List tasks')
    async def list_tasks(
            select: Annotated[TaskSelector | None, Field(
                description='Which tasks. Omit for all of them.')] = None,
            fields: Annotated[list[str] | None, Field(
                description='Fields to return for each task. Default: ' + ', '.join(DEFAULT_FIELDS) +
                            '. Others include command, cores, memory, timeout, outpath, errpath, waited, '
                            'server_host, submit_host, previous_id, next_id, retried, part.')] = None,
            order_by: Annotated[str, Field(description='Field to sort on.')] = 'submit_time',
            descending: Annotated[bool, Field(description='Newest (or largest) first.')] = True,
            limit: Annotated[int, Field(ge=1, description='Most tasks to return.')] = 50,
            offset: Annotated[int, Field(ge=0, description='Tasks to skip, for paging.')] = 0) -> TaskPage:
        """
        List tasks matching a selector, a page at a time. Each task comes with a derived `status`
        (pending, running, succeeded, failed, cancelled). Long commands are shortened here;
        get_task shows one task in full. To count or aggregate, use summarize_tasks instead of
        paging through everything.
        """
        limit = min(limit, limits.rows)
        total, rows = await backend.call(backend.select, select, fields, order_by, descending, limit, offset)
        for row in rows:
            for name in ('args', 'command'):
                value = row.get(name)
                if isinstance(value, str) and len(value) > LONG_TEXT:
                    row[name] = value[:LONG_TEXT] + f'… (+{len(value) - LONG_TEXT} chars)'
        following = offset + len(rows)
        return TaskPage(total=total, offset=offset, count=len(rows), tasks=rows,
                        next_offset=following if following < total else None)

    @tool(mcp, app, Tier.READ, 'Summarize tasks')
    async def summarize_tasks(
            select: Annotated[TaskSelector | None, Field(description='Which tasks. Omit for all.')] = None,
            group_by: Annotated[list[str], Field(
                description='Dimensions to count by, any of: ' + ', '.join(GROUPABLE) +
                            ", 'tag:KEY' for a tag's value, or a time bucket: submitted:, started: or "
                            'completed: with minute, hour or day (e.g. completed:hour). An empty list '
                            'gives one row of totals.')] = ['status'],
            limit: Annotated[int, Field(ge=1, le=500, description='Most rows to return (largest first).')] = 50,
    ) -> TaskSummary:
        """
        Count tasks by any mix of status, group, zone, exit status, host or tag value, with
        duration statistics (seconds) for each row. This answers "how is the campaign going",
        "which hosts are slow", "which parameter values fail" in one call:
        group_by=['tag:model', 'status'], or ['client_host'] with select.status=['failed'].
        A time bucket turns it into a timeline: ['completed:hour', 'status'] shows throughput and
        when failures began.
        """
        return await backend.call(backend.summarize, select, group_by, limit)

    @tool(mcp, app, Tier.READ, 'Get task')
    async def get_task(
            task_id: Annotated[str, Field(description='The task id (UUID).')],
            output_lines: Annotated[int, Field(
                ge=0, le=500, description='Lines of stdout and stderr to include from the end of each '
                                          '(0 for none).')] = 20) -> TaskDetail:
        """
        Everything recorded about one task: the command as submitted and as run, where and when it
        ran, its exit status and what that status means, its tags, the retry chain it belongs to,
        and the end of its output.
        """
        task, attempts = await backend.call(backend.get, task_id)
        stdout = stderr = None
        if output_lines and task['status'] not in ('pending',):
            stdout = await app.task_output(task['id'], 'stdout', lines=output_lines, max_bytes=8192)
            stderr = await app.task_output(task['id'], 'stderr', lines=output_lines, max_bytes=8192)
        return TaskDetail(task=task, attempts=attempts, stdout=stdout, stderr=stderr)

    @tool(mcp, app, Tier.READ, 'Read task output')
    async def get_task_output(
            task_id: Annotated[str, Field(description='The task id (UUID).')],
            stream: Annotated[Literal['stdout', 'stderr'], Field(description='Which stream.')] = 'stdout',
            mode: Annotated[Literal['tail', 'head'], Field(description='Read from the end or the start.')] = 'tail',
            lines: Annotated[int, Field(ge=1, le=5000, description='Most lines to return.')] = 100,
            pattern: Annotated[str | None, Field(
                description='A regular expression: return only matching lines, numbered, instead of '
                            'the head or tail.')] = None) -> OutputChunk:
        """
        Read a task's captured stdout or stderr: its end, its start, or just the lines matching a
        pattern. The result is bounded; `truncated` says when the file holds more. If the output
        is not available the result says why (not captured, not started, on another host).
        """
        return await app.task_output(task_id, stream, mode=mode, lines=lines, pattern=pattern)

    @tool(mcp, app, Tier.READ, 'Diagnose failures')
    async def diagnose_failures(
            ctx: Context,
            select: Annotated[TaskSelector | None, Field(
                description='Narrow to a batch, tag, group or zone. Failed tasks are selected for you.')] = None,
            examine: Annotated[int, Field(
                ge=1, le=2000, description='Most failed tasks to read (newest first).')] = 200) -> FailureReport:
        """
        Group failed tasks by cause. Each failure is reduced to its exit status and the last line
        of its stderr with ids, paths and numbers masked; tasks that share both are one cluster,
        reported with a count, the hosts involved, example ids and one example's stderr. Use this
        before reading individual failures: thousands of failed tasks usually have a handful of
        causes. Attempts already replaced by an automatic retry are left out.
        """
        chosen = (select or TaskSelector()).model_copy(update={'status': ['failed']})
        if chosen.superseded is None:
            chosen = chosen.model_copy(update={'superseded': False})
        total, tasks = await backend.call(backend.select, chosen, ['id', 'args', 'exit_status', 'client_host'],
                                          'completion_time', True, min(examine, 2000), 0)
        tails: dict[str, str | None] = {}
        for index, task in enumerate(tasks):
            chunk = await app.task_output(task['id'], 'stderr', lines=15, max_bytes=4096)
            if chunk.available and not chunk.text.strip():
                chunk = await app.task_output(task['id'], 'stdout', lines=15, max_bytes=4096)
            tails[task['id']] = chunk.text if chunk.available and chunk.text.strip() else None
            if index % 25 == 0:
                await ctx.report_progress(index, len(tasks), 'reading output of failed tasks')
        return build_report(total, tasks, tails)

    @tool(mcp, app, Tier.READ, 'Wait for tasks')
    async def wait_for_tasks(
            ctx: Context,
            select: Annotated[TaskSelector | None, Field(
                description='Which tasks to wait for, usually {"batch": "..."}. Omit to wait for the '
                            'whole queue.')] = None,
            timeout: Annotated[int, Field(
                ge=1, description='Longest to wait, in seconds. The server caps this; when it runs out '
                                  'the result has done=false and you call again.')] = 60) -> WaitResult:
        """
        Block until every matching task has finished, reporting progress as they do. Returns the
        counts by state. It returns early, with a note, if tasks are pending and nothing is running
        them, so a wait never hangs on a queue nobody is serving.
        """
        return await wait_until_done(app, ctx, select, min(timeout, limits.wait))

    @tool(mcp, app, Tier.READ, 'List tags')
    async def list_tags(
            key: Annotated[str | None, Field(
                description='A tag key, to list its values with a count each. Omit to list the keys in use.')] = None,
            select: Annotated[TaskSelector | None, Field(description='Only among these tasks.')] = None,
            limit: Annotated[int, Field(ge=1, le=1000, description='Most keys or values to return.')] = 200,
    ) -> TagIndex:
        """
        Discover how tasks are tagged: the tag keys in use, or the distinct values of one key and
        how many tasks carry each. Useful before filtering, and for finding batches
        (key='batch').
        """
        return await backend.call(backend.tags, key, select, limit)

    @tool(mcp, app, Tier.READ, 'Get configuration')
    async def get_config(
            key: Annotated[str | None, Field(
                description="Dotted name of a value or section, e.g. 'database', 'server.port', "
                            "'task.timeout'. Omit for the whole configuration.")] = None) -> ConfigValue:
        """
        Read HyperShell's effective configuration as this server sees it, with the layer that set
        a value (default, system, user, local or environment). Secrets such as the server auth key
        and database password are masked.
        """
        value, source = await backend.call(backend.config_get, key)
        return ConfigValue(key=key or '', value=value, source=source)

    @tool(mcp, app, Tier.READ, 'List clusters')
    async def list_clusters(
            cluster_id: Annotated[str | None, Field(
                description='One cluster to report on. Omit for the most recent ones.')] = None,
            log_lines: Annotated[int, Field(
                ge=0, le=1000, description='Lines to include from the end of each cluster log.')] = 10,
    ) -> ClusterList:
        """
        The clusters this server started, newest first: whether each is running, how it was
        started, its exit status, and the end of its log. The log is where scheduling problems
        show up (a halted group, clients that cannot connect, a launcher that fails).
        """
        if cluster_id:
            found = [await asyncio.to_thread(app.clusters.get, cluster_id, log_lines)]
        else:
            found = await asyncio.to_thread(app.clusters.list, log_lines, 10)
        note = None if found else 'This server has not started any cluster on this site.'
        return ClusterList(clusters=found, note=note)
