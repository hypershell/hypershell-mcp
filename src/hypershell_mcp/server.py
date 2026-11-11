# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Assembling the server: tools, resources, prompts, authentication and the middleware around them.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from importlib import resources as package_files
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Message
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp_types import Icon
from starlette.requests import Request
from starlette.responses import JSONResponse

import hypershell_mcp
from hypershell_mcp.app import App
from hypershell_mcp.auth import TIERS, Tier, build_authorization, build_verifier
from hypershell_mcp.backend import BackendError, lib
from hypershell_mcp.clusters import ClusterError
from hypershell_mcp.models import TaskSelector
from hypershell_mcp.settings import Settings
from hypershell_mcp.tools import register_all
from hypershell_mcp.tools.read import site_status

__all__ = ['build_server', 'INSTRUCTIONS', 'HELP_COMMANDS']

log = logging.getLogger('hypershell_mcp')

INSTRUCTIONS = """\
HyperShell runs large numbers of independent shell commands (tasks) across many workers. A
database holds the queue; a cluster (a scheduler plus clients) runs what is in it.

Start with `status`. To run something now and get the results back, use `run_tasks`. For a larger
campaign: `submit_tasks` (keep the batch id it returns), `start_cluster`, then `wait_for_tasks`
with {"batch": id}, then `summarize_tasks` and `diagnose_failures`. Every tool that acts on tasks
takes the same `select` object: a batch, ids, status, tags, group, zone, or field conditions.

Tag tasks with whatever you will want to slice by later. Clusters run detached and outlive this
session. The tools you can see are the ones your access allows; `status` names your tiers. The
resource hypershell://guide has the full reference: selectors, templates, groups, zones, exit
statuses.
"""

HELP_COMMANDS = ('cluster', 'server', 'client', 'relay', 'submit', 'list', 'info', 'wait', 'run', 'update',
                 'initdb', 'config')


class Gate(Middleware):
    """
    Wraps every tool call: turns expected failures into messages for the caller, logs unexpected
    ones for the operator, and records each call that could change something in the audit log.
    """

    def __init__(self, app: App) -> None:
        self.app = app

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        name = getattr(context.message, 'name', '?')
        arguments = getattr(context.message, 'arguments', None) or {}
        tier = self.app.tool_tiers.get(name)
        started = time.monotonic()
        error: str | None = None
        try:
            return await call_next(context)
        except (BackendError, ClusterError) as failure:
            error = str(failure)
            raise ToolError(error) from None
        except Exception as failure:
            error = f'{type(failure).__name__}: {failure}'
            if not isinstance(failure, ToolError) and type(failure).__name__ not in (
                    'ValidationError', 'AuthorizationError', 'InsufficientScopeError', 'NotFoundError'):
                log.exception('Tool %s failed', name)
            raise
        finally:
            if tier is not None and tier is not Tier.READ:
                self.app.audit(tool=name, tier=tier, arguments=dict(arguments), ok=error is None, error=error,
                               seconds=time.monotonic() - started)


def build_server(settings: Settings, app: App | None = None) -> FastMCP:
    """
    The MCP server for `settings`.

    Authentication is installed only when the settings hold a key; `Settings.validate` has already
    refused HTTP without one unless it was explicitly waived. Tiers the settings switch off are
    removed from the server altogether, so no token can reach them.
    """
    app = app or App(settings=settings)
    secured = settings.auth.enabled

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            app.backend.close()

    mcp = FastMCP(
        name='HyperShell',
        instructions=INSTRUCTIONS,
        version=hypershell_mcp.__version__,
        website_url='https://hypershell.org',
        icons=[Icon(src='https://hypershell.readthedocs.io/_static/logo-dark-mode.png', mime_type='image/png')],
        auth=build_verifier(settings.auth) if secured else None,
        middleware=[Gate(app), *([build_authorization()] if secured else [])],
        lifespan=lifespan,
        mask_error_details=True,
        on_duplicate='error',
    )
    mcp.state = app  # type: ignore[attr-defined]  # for tests and embedding

    register_all(mcp, app)
    register_resources(mcp, app)
    register_prompts(mcp)

    for tier in TIERS:
        if tier not in settings.tiers:
            mcp.disable(tags={tier.value})

    @mcp.custom_route('/health', methods=['GET'])
    async def health(request: Request) -> JSONResponse:
        """Liveness for load balancers and monitors. Unauthenticated, so it says nothing of the site."""
        return JSONResponse({'status': 'ok', 'server': 'hypershell-mcp', 'version': hypershell_mcp.__version__})

    return mcp


def register_resources(mcp: FastMCP, app: App) -> None:
    """Things a client can attach as context without calling a tool."""
    backend = app.backend
    read = {Tier.READ.value}

    @mcp.resource('hypershell://guide', name='guide', title='HyperShell guide', mime_type='text/markdown', tags=read,
                  description='How to work with HyperShell through this server: the workflow, selectors, '
                              'templates, tags, groups, zones and exit statuses.')
    def guide() -> str:
        return package_files.files('hypershell_mcp').joinpath('guide.md').read_text()

    @mcp.resource('hypershell://status', name='status', title='Site status', mime_type='application/json',
                  tags=read, description='Task counts, the queue, running clusters and your access, as JSON.')
    async def status() -> dict[str, Any]:
        return (await site_status(app, None)).model_dump()

    @mcp.resource('hypershell://config', name='config', title='Configuration', mime_type='application/json',
                  tags=read, description='The effective HyperShell configuration with secrets masked.')
    async def config() -> dict[str, Any]:
        value, _ = await backend.call(backend.config_get, None)
        return value

    @mcp.resource('hypershell://help/{command}', name='help', title='hs command help', mime_type='text/plain',
                  tags=read, description="The hs command line's own help for one command: " +
                                         ', '.join(HELP_COMMANDS) + '.')
    async def command_help(command: str) -> str:
        if command not in HELP_COMMANDS:
            raise ValueError(f"no help for {command!r}; commands are {', '.join(HELP_COMMANDS)}")
        _, text = await app.hs(command, '--help', timeout=30)
        return text

    @mcp.resource('hypershell://task/{task_id}', name='task', title='Task', mime_type='application/json', tags=read,
                  description='Everything recorded about one task.')
    async def task(task_id: str) -> dict[str, Any]:
        record, attempts = await backend.call(backend.get, task_id)
        return {'task': record, 'attempts': attempts}

    @mcp.resource('hypershell://task/{task_id}/{stream}', name='task-output', title='Task output',
                  mime_type='text/plain', tags=read,
                  description="The end of a task's captured output; stream is stdout or stderr.")
    async def task_output(task_id: str, stream: str) -> str:
        if stream not in ('stdout', 'stderr'):
            raise ValueError("stream must be 'stdout' or 'stderr'")
        chunk = await app.task_output(task_id, stream, lines=1000)
        return chunk.text if chunk.available else f'[{chunk.reason}]'

    @mcp.resource('hypershell://batch/{batch}', name='batch', title='Batch summary', mime_type='application/json',
                  tags=read, description='Counts by state and exit status for one batch.')
    async def batch(batch: str) -> dict[str, Any]:
        selector = TaskSelector(batch=batch)
        counts = await backend.call(backend.count, selector)
        summary = await backend.call(backend.summarize, selector, ['status', 'exit_status'], 50)
        return {'batch': batch, 'counts': counts.model_dump(), 'by_exit_status': summary.model_dump()['rows']}

    @mcp.resource('hypershell://cluster/{cluster_id}/log', name='cluster-log', title='Cluster log',
                  mime_type='text/plain', tags=read, description='The end of the log of a cluster this server started.')
    def cluster_log(cluster_id: str) -> str:
        try:
            return '\n'.join(app.clusters.log(cluster_id, 500))
        except ClusterError as error:
            raise ValueError(str(error)) from None


def register_prompts(mcp: FastMCP) -> None:
    """Starting points for the workflows this server is for."""
    read = {Tier.READ.value}

    @mcp.prompt(name='parameter_sweep', title='Run a parameter sweep', tags=read,
                description='Plan, submit and run one command over a grid of parameter values.')
    def parameter_sweep(command: str, parameters: str) -> list[Message]:
        return [Message(
            f'Run a parameter sweep with HyperShell.\n\nCommand: {command}\nParameters to vary: {parameters}\n\n'
            'Read hypershell://guide if you have not. Then:\n'
            '1. Expand the grid into one command per combination and tell me how many tasks that is. If it is '
            'more than a few hundred, confirm with me before submitting.\n'
            '2. Submit them as task objects so each carries its parameter values as tags (one tag per '
            'parameter). Add a tag naming the sweep.\n'
            '3. If there are a handful and they are quick, use run_tasks. Otherwise submit_tasks, then '
            'start_cluster if nothing is serving the queue, then wait_for_tasks on the batch.\n'
            "4. Report with summarize_tasks grouped by each parameter's tag and status: which values "
            'succeeded, which failed, and how long they took. For failures, run diagnose_failures on the '
            'batch and say what the cause is.')]

    @mcp.prompt(name='triage_failures', title='Triage failed tasks', tags=read,
                description='Find out why tasks failed and what to do about each cause.')
    def triage_failures(batch: str = '') -> list[Message]:
        scope = f'the batch {batch}' if batch else 'this site'
        selector = f'{{"batch": "{batch}"}}' if batch else 'no selector'
        return [Message(
            f'Triage the failed HyperShell tasks for {scope}.\n\n'
            f'1. Call diagnose_failures ({selector}). Work from its clusters, not from individual tasks.\n'
            '2. For each cluster, say in one line what went wrong and whether the fault is in the task '
            '(bad input, a bug), the environment (a missing command or module, a bad node) or a limit '
            '(memory, wall time). Use get_task and get_task_output on one example where the signature is '
            'not enough.\n'
            '3. Check whether failures concentrate on particular hosts or tag values with summarize_tasks '
            '(group_by client_host, or the relevant tag).\n'
            '4. Recommend an action per cluster: retry as is (transient), retry after a fix (say what '
            'fix), or leave failed (the task itself is wrong). Do not retry anything until I agree.')]

    @mcp.prompt(name='campaign_report', title='Report on a campaign', tags=read,
                description='Summarize how a batch, or the whole site, has gone.')
    def campaign_report(batch: str = '') -> list[Message]:
        scope = f'batch {batch}' if batch else 'everything in the database'
        return [Message(
            f'Write a short report on {scope} in HyperShell.\n\n'
            'Use status for the overall picture and summarize_tasks for the breakdowns that matter here: '
            'by status, by group if there are several, by client_host for throughput, and by any tag that '
            'looks like a parameter (list_tags shows the keys). Give: how many tasks finished and how they '
            'ended; total and typical run time; what is still running or pending and whether anything is '
            'serving it; and the causes of failure from diagnose_failures. Lead with the state of the '
            'campaign in one sentence. Numbers in a small table; no narration of the calls you made.')]

    @mcp.prompt(name='resume_campaign', title='Resume an interrupted campaign', tags=read,
                description='Pick up after a cluster died, a node failed or a job hit its time limit.')
    def resume_campaign() -> list[Message]:
        return [Message(
            'A HyperShell campaign was interrupted and I want to pick it up.\n\n'
            '1. Call status. Report what is pending, what is marked running with no client connected (those '
            'were interrupted), and what failed.\n'
            '2. Check list_clusters for how the last cluster ended and what its log says.\n'
            '3. Tasks left marked running are re-queued automatically by the next cluster to start. Failed '
            'tasks are not: run diagnose_failures and tell me which failures look like casualties of the '
            'interruption (signals, the same moment, the same host) and which are real.\n'
            '4. Propose the restart: which failures to retry_tasks, and the start_cluster options. Wait for '
            'my go-ahead before changing anything.')]


def describe(settings: Settings) -> dict[str, Any]:
    """What the server would serve, for `hypershell-mcp check`."""
    hs = lib()
    return {'hypershell': hs.version, 'server': hypershell_mcp.__version__,
            'transport': settings.transport, 'tiers': [tier.value for tier in TIERS if tier in settings.tiers],
            'authentication': (f'JWT {settings.auth.resolved_algorithm}' if settings.auth.enabled
                               else 'none (local user)' if settings.transport == 'stdio' else 'NONE'),
            'issuer': settings.auth.issuer, 'audience': settings.auth.audience}
