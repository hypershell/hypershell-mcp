# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
The shapes tools accept and return.

Tool results are typed so that FastMCP publishes an output schema for each tool and a client can
rely on the structure. Field descriptions here are what a model reads when it decides how to call a
tool, so they say what a value means, not only what type it has.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ['State', 'STATES', 'TagValue', 'TaskSelector', 'TaskSpec', 'StatusCounts', 'TaskPage',
           'TaskDetail', 'OutputChunk', 'SubmitResult', 'ChangeResult', 'TaskGroupRow', 'TaskSummary',
           'FailureCluster', 'FailureReport', 'WaitResult', 'RunResult', 'TaskResult', 'ClusterInfo',
           'ClusterList', 'SiteStatus', 'DatabaseInfo', 'QueueInfo', 'AccessInfo', 'ConfigValue',
           'AdminResult', 'TagIndex']

State = Literal['pending', 'running', 'succeeded', 'failed', 'cancelled', 'completed', 'remaining']
STATES: tuple[str, ...] = ('pending', 'running', 'succeeded', 'failed', 'cancelled')
TagValue = str | int | float | bool | None


class Model(BaseModel):
    """Base for results: unknown fields are an error, so a typo never passes silently."""

    model_config = ConfigDict(extra='forbid')


class TaskSelector(Model):
    """
    Which tasks a call applies to. Every field given must match (they are ANDed).

    An empty selector matches every task in the database, which is what listing and counting
    want; tools that change tasks refuse an empty selector unless told `everything=true`.
    """

    ids: Annotated[list[str] | None, Field(
        description='Exact task ids (UUIDs).')] = None
    batch: Annotated[str | None, Field(
        description='A batch id returned by submit_tasks or run_tasks. Shorthand for the tag '
                    'batch:<id>.')] = None
    status: Annotated[list[State] | None, Field(
        description='Any of these states. pending: not yet handed to a client. running: handed '
                    'out and not finished. succeeded: exit status 0. failed: finished with a '
                    'non-zero exit status, killed by a signal, or timed out. cancelled: cancelled '
                    'before it finished. completed: any finished state. remaining: pending or '
                    'running.')] = None
    tags: Annotated[dict[str, TagValue] | None, Field(
        description='Tags the task must carry. A value of null means the tag only has to be '
                    'present; otherwise the value must be equal.')] = None
    group: Annotated[int | None, Field(
        description='Task group. Groups run in ascending order, each finishing before the next '
                    'starts.')] = None
    zone: Annotated[str | None, Field(
        description="Zone name. 'default' selects tasks that were submitted without a zone.")] = None
    where: Annotated[list[str] | None, Field(
        description="Extra conditions as 'FIELD OP VALUE', all of which must hold. OP is one of "
                    "== != > >= < <= ~ (regular expression) !~. VALUE null tests for absence. "
                    "Examples: 'exit_status == 137', 'duration > 600', 'args ~ \\.fastq$', "
                    "'client_host == node042', 'submit_time >= 2026-11-01'.")] = None
    superseded: Annotated[bool | None, Field(
        description='false leaves out failed attempts that an automatic retry already replaced, '
                    'so each task is counted once by its latest attempt; true selects only those '
                    'replaced attempts. Omit to include both.')] = None

    def is_empty(self) -> bool:
        return not any((self.ids, self.batch, self.status, self.tags, self.group is not None,
                        self.zone, self.where, self.superseded is not None))


class TaskSpec(Model):
    """One task with its own options, for when tasks in a submission differ."""

    command: Annotated[str, Field(description='The shell command line, stored exactly as given.')]
    tags: Annotated[dict[str, TagValue] | None, Field(
        description='Tags for this task, merged over the tags given for the whole submission.')] = None
    group: Annotated[int | None, Field(description='Task group for this task.')] = None
    cores: Annotated[int | None, Field(description='Cores this task needs.', ge=1)] = None
    memory: Annotated[str | None, Field(description="Memory this task needs, e.g. '4GB'.")] = None
    timeout: Annotated[int | None, Field(description='Wall-time limit in seconds.', ge=1)] = None


class StatusCounts(Model):
    """How many tasks are in each state."""

    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    total: int = 0

    @property
    def remaining(self) -> int:
        return self.pending + self.running

    @property
    def done(self) -> bool:
        return self.total > 0 and self.remaining == 0


class TaskPage(Model):
    """One page of tasks."""

    total: Annotated[int, Field(description='Tasks matching the selector, across all pages.')]
    offset: int
    count: Annotated[int, Field(description='Tasks on this page.')]
    next_offset: Annotated[int | None, Field(
        description='Pass as offset to get the next page; null when this is the last page.')] = None
    tasks: Annotated[list[dict[str, Any]], Field(
        description='One object per task with the requested fields.')]


class TaskDetail(Model):
    """Everything recorded about one task."""

    task: Annotated[dict[str, Any], Field(description='Every column of the task, plus its derived '
                                                      'status, status_detail and zone name.')]
    attempts: Annotated[list[dict[str, Any]], Field(
        description='The retry chain this task belongs to, oldest first, when it was retried.')] = []
    stdout: OutputChunk | None = None
    stderr: OutputChunk | None = None


class OutputChunk(Model):
    """Part of a task's captured stdout or stderr."""

    task_id: str
    stream: Literal['stdout', 'stderr']
    available: Annotated[bool, Field(description='Whether the output could be read here.')]
    reason: Annotated[str | None, Field(
        description='Why the output is not available, when it is not.')] = None
    text: str = ''
    size: Annotated[int, Field(description='Size of the whole output file in bytes.')] = 0
    lines_returned: int = 0
    truncated: Annotated[bool, Field(
        description='True when the file holds more than was returned.')] = False
    matches: Annotated[int | None, Field(
        description='Lines that matched the pattern, when one was given.')] = None


class SubmitResult(Model):
    """What a submission created."""

    batch: Annotated[str, Field(
        description='Handle for this submission: pass it as selector.batch to wait for, list, '
                    'cancel or diagnose exactly these tasks.')]
    submitted: int
    group: Annotated[int | None, Field(description='The group the tasks went into, when they share one.')]
    zone: str
    ids: Annotated[list[str], Field(description='The new task ids (the first 100 when there are more).')]
    note: str | None = None


class ChangeResult(Model):
    """The effect of a change to existing tasks."""

    action: str
    dry_run: Annotated[bool, Field(description='True when nothing was changed and this is a preview.')]
    matched: Annotated[int, Field(description='Tasks the selector matched.')]
    changed: Annotated[int, Field(description='Tasks changed (0 on a dry run).')]
    skipped: Annotated[int, Field(description='Matched tasks left alone; see note for why.')] = 0
    sample_ids: Annotated[list[str], Field(description='Up to 20 of the affected task ids.')] = []
    note: str | None = None


class TaskGroupRow(Model):
    """One row of a summary: a combination of group_by values and its statistics."""

    key: Annotated[dict[str, Any], Field(description='The group_by values this row is for.')]
    count: int
    duration_mean: float | None = None
    duration_min: float | None = None
    duration_max: float | None = None
    duration_total: float | None = None
    waited_mean: float | None = None


class TaskSummary(Model):
    """Tasks counted by one or more dimensions."""

    total: int
    group_by: list[str]
    rows: list[TaskGroupRow]
    truncated: Annotated[bool, Field(description='True when there were more rows than returned.')] = False


class FailureCluster(Model):
    """Failed tasks that failed the same way."""

    exit_status: int | None
    meaning: Annotated[str, Field(description='What that exit status usually means.')]
    signature: Annotated[str | None, Field(
        description='The last line of stderr with numbers, ids and paths masked, shared by the '
                    'tasks in this cluster.')]
    count: int
    hosts: Annotated[dict[str, int], Field(description='Failures per client host.')] = {}
    example_ids: list[str] = []
    example_command: str | None = None
    example_stderr: Annotated[str | None, Field(description='The end of one task\'s stderr.')] = None


class FailureReport(Model):
    """Failures grouped by cause."""

    failed: Annotated[int, Field(description='Failed tasks matching the selector.')]
    examined: Annotated[int, Field(description='How many of them were read for this report.')]
    clusters: list[FailureCluster]
    advice: list[str] = []


class WaitResult(Model):
    """Where a set of tasks stood when the wait ended."""

    done: Annotated[bool, Field(
        description='True when every matching task has finished. When false the wait timed out; '
                    'call again to keep waiting.')]
    counts: StatusCounts
    waited_seconds: float
    note: str | None = None


class TaskResult(Model):
    """The outcome of one task in a run."""

    id: str
    command: str
    status: str
    exit_status: int | None = None
    duration: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False


class RunResult(Model):
    """The outcome of run_tasks."""

    batch: str
    done: bool
    counts: StatusCounts
    waited_seconds: float
    cluster: Annotated[str | None, Field(description='Id of the cluster that ran the tasks.')] = None
    results: Annotated[list[TaskResult], Field(
        description='Per-task outcomes in submission order (the first 50 when there are more).')] = []
    note: str | None = None


class ClusterInfo(Model):
    """A cluster this server started."""

    id: str
    state: Literal['running', 'exited']
    pid: int
    started: str
    stopped: str | None = None
    exit_code: int | None = None
    mode: Annotated[str, Field(description="'drain' exits when the queue is empty; 'forever' "
                                           'keeps waiting for new tasks.')]
    workers: Annotated[int, Field(description='Tasks run at once on each client.')]
    launcher: str | None = None
    zone: str | None = None
    cwd: Annotated[str | None, Field(description='Directory the tasks run in.')] = None
    command: Annotated[str, Field(description='The hs command line that was started.')]
    log_tail: Annotated[list[str], Field(description='The last lines of the cluster log.')] = []


class ClusterList(Model):
    """Clusters started by this server."""

    clusters: list[ClusterInfo]
    note: str | None = None


class DatabaseInfo(Model):
    """The database this server is bound to."""

    provider: str
    location: Annotated[str, Field(description='File path, or host and database name.')]
    size_bytes: int | None = None
    partitions: Annotated[int, Field(description='Rotated partition files beside the main database.')] = 0


class QueueInfo(Model):
    """What the scheduler would do next."""

    current_group: Annotated[int | None, Field(description='The group being worked on now.')] = None
    groups_remaining: Annotated[dict[str, int], Field(
        description='Unfinished tasks per group (as strings; lowest runs first).')] = {}
    zones_remaining: Annotated[dict[str, int], Field(description='Unfinished tasks per zone.')] = {}
    clients_connected: Annotated[int, Field(
        description='Clients connected to the most recent server, by its own record.')] = 0
    tasks_per_second: float | None = None
    seconds_to_completion: float | None = None
    superseded_attempts: Annotated[int, Field(
        description='Failed attempts that a retry has since replaced (they are in the failed count).')] = 0


class AccessInfo(Model):
    """Who is calling and what they may do."""

    subject: Annotated[str | None, Field(description='The token subject; null over stdio.')] = None
    transport: str
    tiers: Annotated[list[str], Field(
        description='Tiers of tools available to this caller: read, submit, manage, run, admin.')]
    token_expires: str | None = None


class SiteStatus(Model):
    """One look at the whole site."""

    hypershell_version: str
    server_version: str
    host: str
    database: DatabaseInfo
    tasks: StatusCounts
    queue: QueueInfo
    clusters: Annotated[list[ClusterInfo], Field(description='Clusters this server started that '
                                                             'are still running.')] = []
    access: AccessInfo
    hints: Annotated[list[str], Field(description='What looks worth doing next, if anything.')] = []


class ConfigValue(Model):
    """A configuration value and where it came from."""

    key: str
    value: Any
    source: Annotated[str | None, Field(
        description='Which layer set it: default, system, user, local, env.')] = None


class AdminResult(Model):
    """The outcome of a database or configuration operation."""

    action: str
    ok: bool
    detail: str
    path: str | None = None


class TagIndex(Model):
    """Tag keys in use, or the values of one key."""

    key: str | None = None
    keys: list[str] = []
    values: Annotated[dict[str, int], Field(description='Value → number of tasks, for one key.')] = {}
    truncated: bool = False
