# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Submit tier (add tasks) and manage tier (cancel, retry, tag and delete existing tasks)."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.backend import TEMPLATE_HELP, BackendError
from hypershell_mcp.models import ChangeResult, SubmitResult, TagValue, TaskSelector, TaskSpec
from hypershell_mcp.tools import tool

__all__ = ['register', 'submit']

Commands = Annotated[list[str | TaskSpec], Field(
    min_length=1,
    description='The tasks: each a shell command line, or an object {"command": ..., "tags": ..., '
                '"group": ..., "cores": ..., "memory": ..., "timeout": ...} when one task needs its '
                'own options. Commands are stored exactly as written.')]
Template = Annotated[str | None, Field(
    description="Build each command from an input: e.g. template='python fit.py --input {}' with "
                'commands holding the inputs. ' + TEMPLATE_HELP + '.')]
Tags = Annotated[dict[str, TagValue] | None, Field(
    description='Tags for every task, e.g. {"project": "atlas", "stage": "align"}. Keys and string '
                'values use letters, digits and _ . + -')]


Select = Annotated[TaskSelector, Field(description='Which tasks to act on.')]
DryRun = Annotated[bool, Field(description='Report what would change without changing it.')]
Everything = Annotated[bool, Field(
    description='Required to act on the whole database when the selector is empty.')]


async def submit(app: App, commands: list[str | TaskSpec], *, template: str | None,
                 tags: dict[str, Any] | None, group: int | None, zone: str | None, cores: int | None,
                 memory: str | None, timeout: int | None, batch: str | None,
                 dry_run: bool = False) -> SubmitResult:
    """Submit tasks for the current caller (shared by submit_tasks and run_tasks)."""
    limit = app.settings.limits.submit
    if len(commands) > limit:
        raise BackendError(f'{len(commands)} tasks is more than one call accepts ({limit}). Submit in '
                           'several calls, passing the same batch id to keep them together.')
    created = await app.backend.call(
        app.backend.submit, commands, template=template, tags=tags, group=group, zone=zone, cores=cores,
        memory=memory, timeout=timeout, batch=batch, submitter=app.caller().subject, dry_run=dry_run)
    ids = created.pop('ids')
    note = f'Showing the first 100 of {len(ids)} ids; list_tasks with this batch returns the rest.' if len(ids) > 100 else None
    return SubmitResult(**created, ids=ids[:100], note=note)


def register(mcp: FastMCP, app: App) -> None:
    backend = app.backend

    @tool(mcp, app, Tier.SUBMIT, 'Submit tasks')
    async def submit_tasks(
            commands: Commands,
            template: Template = None,
            tags: Tags = None,
            group: Annotated[int | None, Field(
                description='Task group. Lower groups run first and a group finishes before the next '
                            'starts, so stages of a pipeline go in groups 0, 1, 2. Default: the group '
                            'now being worked on.')] = None,
            zone: Annotated[str | None, Field(
                description="Run only on clients of this zone, e.g. 'gpu'. Default: clients without a zone.")] = None,
            cores: Annotated[int | None, Field(ge=1, description='Cores each task needs.')] = None,
            memory: Annotated[str | None, Field(description="Memory each task needs, e.g. '4GB'.")] = None,
            timeout: Annotated[int | None, Field(ge=1, description='Wall-time limit per task, in seconds.')] = None,
            batch: Annotated[str | None, Field(
                description='Batch id to file these tasks under, to add to an earlier submission. '
                            'Default: a new id.')] = None,
            dry_run: Annotated[bool, Field(
                description='Check the submission and show the first commands as they would be '
                            'stored, without adding anything. Worth doing before a large templated '
                            'submission.')] = False) -> SubmitResult:
        """
        Add tasks to the queue. They run when a cluster serves the queue: one already running,
        one you start with start_cluster, or one someone starts elsewhere against this database.
        To submit and run in one step, use run_tasks.

        Returns a `batch` id. Keep it: {"batch": id} selects exactly these tasks in every other
        tool (wait_for_tasks, list_tasks, summarize_tasks, diagnose_failures, cancel_tasks).
        Tags are how you find and group tasks later, so tag anything you will want to slice by,
        such as a parameter value.
        """
        return await submit(app, commands, template=template, tags=tags, group=group, zone=zone, cores=cores,
                            memory=memory, timeout=timeout, batch=batch, dry_run=dry_run)

    @tool(mcp, app, Tier.MANAGE, 'Cancel tasks', idempotent=True)
    async def cancel_tasks(select: Select, dry_run: DryRun = False, everything: Everything = False) -> ChangeResult:
        """
        Cancel unfinished tasks so they will not be scheduled or retried. A pending task never
        starts. A task already running on a client is not interrupted: it runs to its end and its
        result is discarded (to stop running work, stop the cluster). Finished tasks are skipped.
        A cancelled task can be re-queued with retry_tasks.
        """
        return ChangeResult(**await backend.call(backend.change, 'cancel', select, dry_run=dry_run,
                                                 everything=everything))

    @tool(mcp, app, Tier.MANAGE, 'Retry tasks')
    async def retry_tasks(select: Select, dry_run: DryRun = False, everything: Everything = False) -> ChangeResult:
        """
        Put finished tasks back in the queue as if new, forgetting their previous result. When the
        selector names neither ids nor a status, only failed tasks are retried: say
        status=['cancelled'] or ['succeeded'] to re-run those. Running tasks are skipped, and so
        are attempts an automatic retry already replaced. Fix the cause first: diagnose_failures
        tells you what it was.
        """
        if not select.ids and not select.status:
            select = select.model_copy(update={'status': ['failed']})
            everything = True  # 'every failed task' is a bounded, intended meaning
        return ChangeResult(**await backend.call(backend.change, 'retry', select, dry_run=dry_run,
                                                 everything=everything))

    @tool(mcp, app, Tier.MANAGE, 'Tag tasks', idempotent=True)
    async def tag_tasks(
            select: Select,
            set: Annotated[dict[str, TagValue] | None, Field(
                description='Tags to add or overwrite, e.g. {"reviewed": true, "attempt_note": "oom"}.')] = None,
            remove: Annotated[list[str] | None, Field(description='Tag keys to remove.')] = None,
            dry_run: DryRun = False, everything: Everything = False) -> ChangeResult:
        """
        Add, change or remove tags on existing tasks. Tags are free-form labels: use them to mark
        tasks as reviewed, to record why something was retried, or to group tasks you found by
        other means so you can select them again.
        """
        return ChangeResult(**await backend.call(backend.change, 'tag', select, dry_run=dry_run,
                                                 everything=everything, set_tags=set, remove_tags=remove))

    @tool(mcp, app, Tier.MANAGE, 'Delete tasks', destructive=True)
    async def delete_tasks(
            select: Select,
            confirm: Annotated[bool, Field(
                description='Must be true to delete. Without it the call is a preview: it reports '
                            'how many tasks would go and changes nothing.')] = False,
            everything: Everything = False) -> ChangeResult:
        """
        Remove tasks from the database for good: their records, tags and results. This cannot be
        undone, and output files already written are left where they are. Call it once without
        confirm to see what would be deleted, then again with confirm=true. Running tasks are
        skipped. To stop tasks but keep their records, use cancel_tasks.
        """
        result = ChangeResult(**await backend.call(backend.change, 'delete', select, dry_run=not confirm,
                                                   everything=everything))
        if not confirm:
            result.note = (f'Preview only: {result.matched - result.skipped} tasks would be deleted. '
                           'Call again with confirm=true to delete them. ' + (result.note or ''))
        return result
