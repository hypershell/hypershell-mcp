# SPDX-FileCopyrightText: 2025 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
HyperShell MCP Server implementation.

This module provides MCP tools for interacting with HyperShell, a distributed
task execution system for high-performance computing.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from mcp.types import Icon

# HyperShell imports
from hypershell.core.config import config, default, load_file, update
from hypershell.core.platform import path
from hypershell.data import initdb, ensuredb, checkdb, truncatedb, vacuumdb
from hypershell.data import DATABASE_ENABLED, DATABASE_PROVIDER, DATABASE_DIALECT
from hypershell.data.model import Task
from hypershell.submit import submit_from


# Create the MCP server
mcp = FastMCP(
    name="HyperShell",
    website_url="https://hypershell.org",
    icons=[
        Icon(
            src="https://hypershell.readthedocs.io/_static/logo-dark-mode.png",
            mimeType="image/png",
        ),
    ],
    instructions="""
    HyperShell MCP Server provides tools for distributed task execution.

    HyperShell is a cross-platform, high-throughput computing utility for
    processing shell commands over a distributed, asynchronous queue.

    Key concepts:
    - Tasks: Shell commands to be executed
    - Server: Central coordinator with database (SQLite or PostgreSQL)
    - Clients: Workers that execute tasks
    - Tags: Key-value pairs for annotating tasks

    Common workflows:
    1. Initialize database with init_database()
    2. Submit tasks with submit_tasks()
    3. Monitor with get_task_info() or list_tasks()
    4. Check status with get_database_stats()
    """
)


# =============================================================================
# Configuration Tools
# =============================================================================

@mcp.tool
def get_config(
    varpath: str = ".",
    site: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get HyperShell configuration values.

    Args:
        varpath: Dot-separated path to configuration variable (e.g., "database.file").
                 Use "." to get entire configuration.
        site: Configuration source - one of "system", "user", "local", "default", or None for merged.

    Returns:
        Configuration value or section as a dictionary.

    Examples:
        get_config("database") - Get database configuration section
        get_config("server.port") - Get server port setting
        get_config(".", "default") - Get all default configuration values
    """
    if site == "default":
        cfg = default
    elif site is not None:
        config_path = path[site].config
        cfg = load_file(config_path)
    else:
        cfg = config

    if varpath == ".":
        return dict(cfg)

    parts = varpath.split(".")
    value = cfg
    for part in parts:
        if hasattr(value, part):
            value = getattr(value, part)
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ValueError(f"Configuration path '{varpath}' not found")

    if hasattr(value, 'to_dict'):
        return value.to_dict()
    elif isinstance(value, dict):
        return dict(value)
    else:
        return {"value": value}


@mcp.tool
def set_config(
    varpath: str,
    value: Any,
    site: str = "user"
) -> Dict[str, str]:
    """
    Set a HyperShell configuration value.

    Args:
        varpath: Dot-separated path to configuration variable (e.g., "database.file").
        value: Value to set (will be type-coerced appropriately).
        site: Configuration site - one of "system", "user", or "local". Defaults to "user".

    Returns:
        Confirmation message.

    Examples:
        set_config("database.file", "/path/to/tasks.db")
        set_config("server.port", 50001)
        set_config("logging.level", "debug")
    """
    if "." not in varpath:
        raise ValueError("Configuration path must include section (e.g., 'database.file')")

    section, *subsections, variable = varpath.split(".")

    cfg: Dict[str, Any] = {section: {}}
    config_section = cfg[section]
    for subsection in subsections:
        config_section[subsection] = {}
        config_section = config_section[subsection]

    config_section[variable] = value
    update(site, cfg)

    return {"status": "success", "message": f"Set {varpath} = {value} in {site} configuration"}


@mcp.tool
def get_config_path(site: str = "user") -> Dict[str, str]:
    """
    Get the file path for a configuration site.

    Args:
        site: Configuration site - one of "system", "user", or "local".

    Returns:
        File path for the configuration file.
    """
    return {"path": path[site].config}


# =============================================================================
# Database Tools
# =============================================================================

@mcp.tool
def init_database(optimize: bool = False) -> Dict[str, str]:
    """
    Initialize the HyperShell database.

    For SQLite databases, this creates the database file and tables automatically.
    For PostgreSQL, this creates the required tables in the configured database.

    Args:
        optimize: If True, run SQLite PRAGMA optimize after initialization.

    Returns:
        Status message with database information.
    """
    initdb(optimize=optimize)
    return {
        "status": "success",
        "provider": DATABASE_PROVIDER,
        "dialect": DATABASE_DIALECT,
        "enabled": str(DATABASE_ENABLED),
    }


@mcp.tool
def get_database_stats() -> Dict[str, Any]:
    """
    Get statistics about the HyperShell task database.

    Returns:
        Dictionary containing task counts and database information.
    """
    ensuredb()

    total = Task.count()
    remaining = Task.count_remaining()
    interrupted = Task.count_interrupted()
    completed = total - remaining

    return {
        "provider": DATABASE_PROVIDER,
        "dialect": DATABASE_DIALECT,
        "tasks": {
            "total": total,
            "completed": completed,
            "remaining": remaining,
            "interrupted": interrupted,
        }
    }


@mcp.tool
def vacuum_database(backup_path: Optional[str] = None) -> Dict[str, str]:
    """
    Vacuum the database to reclaim space and optimize performance.

    For SQLite, this can optionally create a backup at the specified path.

    Args:
        backup_path: Optional path to create a backup (SQLite only).

    Returns:
        Status message.
    """
    ensuredb()
    vacuumdb(backup_path)

    if backup_path:
        return {"status": "success", "message": f"Database vacuumed into backup at {backup_path}"}
    return {"status": "success", "message": "Database vacuumed"}


@mcp.tool
def truncate_database(confirm: bool = False) -> Dict[str, str]:
    """
    Truncate (clear) all tasks from the database.

    WARNING: This permanently deletes all task data!

    Args:
        confirm: Must be True to confirm the destructive operation.

    Returns:
        Status message.
    """
    if not confirm:
        return {
            "status": "error",
            "message": "Set confirm=True to truncate the database. This will delete all task data!"
        }

    ensuredb()
    count = Task.count()
    truncatedb()

    return {"status": "success", "message": f"Truncated database. Removed {count} tasks."}


# =============================================================================
# Task Submission Tools
# =============================================================================

@mcp.tool
def submit_tasks(
    commands: List[str],
    tags: Optional[Dict[str, Any]] = None,
    template: str = "{}",
    group: int = 0,
) -> Dict[str, Any]:
    """
    Submit tasks (shell commands) to the HyperShell database for execution.

    Args:
        commands: List of shell commands to execute.
        tags: Optional key-value tags to attach to all tasks (e.g., {"project": "analysis"}).
        template: Template string where {} is replaced with each command. Default is "{}".
        group: Task group number for ordering execution. Default is 0.

    Returns:
        Summary of submitted tasks.

    Examples:
        submit_tasks(["echo hello", "echo world"])
        submit_tasks(["file1.txt", "file2.txt"], template="process {}")
        submit_tasks(["task1", "task2"], tags={"batch": "morning"}, group=1)

    Notes:
        Be aware that tags can be inlined using the template paramater. Bulk tags
        that apply to every task in the set can be specified with tags={...},
        but individual tasks can be applied as template="some {} HYPERSHELL: n:{}".
        Other options like cores, memory, and group are special cases that can also
        be inlined as a special #HYPERSHELL: comment.
    """
    ensuredb()

    count = submit_from(
        source=commands,
        template=template,
        tags=tags,
        group=group,
    )

    return {
        "status": "success",
        "submitted": count,
        "group": group,
        "template": template,
        "tags": tags or {},
    }


@mcp.tool
def submit_task(
    command: str,
    tags: Optional[Dict[str, Any]] = None,
    group: int = 0,
) -> Dict[str, Any]:
    """
    Submit a single task (shell command) to the HyperShell database.

    Args:
        command: Shell command to execute.
        tags: Optional key-value tags to attach to the task.
        group: Task group number for ordering execution. Default is 0.

    Returns:
        Information about the submitted task including its ID.
    """
    ensuredb()

    task = Task.new(args=command, tag=tags, group=group)
    Task.add(task)

    return {
        "status": "success",
        "task_id": task.id,
        "command": command,
        "group": group,
        "tags": tags or {},
    }


# =============================================================================
# Task Query Tools
# =============================================================================

@mcp.tool
def get_task_info(task_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific task.

    Args:
        task_id: The unique UUID of the task.

    Returns:
        Complete task metadata including status, timing, and outputs.
    """
    ensuredb()
    task = Task.from_id(task_id)
    return task.to_json()


@mcp.tool
def list_tasks(
    limit: int = 100,
    status: Optional[str] = None,
    tag_filter: Optional[Dict[str, Any]] = None,
    group: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List tasks from the database with optional filters.

    Args:
        limit: Maximum number of tasks to return. Default is 100.
        status: Filter by status - "pending", "running", "completed", "failed", or None for all.
        tag_filter: Filter by tags (tasks must have all specified tags).
        group: Filter by task group number.

    Returns:
        List of tasks matching the criteria.
    """
    ensuredb()

    query = Task.query().order_by(Task.submit_time.desc())

    # Apply status filter
    if status == "pending":
        query = query.filter(Task.schedule_time.is_(None))
    elif status == "running":
        query = query.filter(Task.schedule_time.isnot(None))
        query = query.filter(Task.completion_time.is_(None))
    elif status == "completed":
        query = query.filter(Task.completion_time.isnot(None))
        query = query.filter(Task.exit_status == 0)
    elif status == "failed":
        query = query.filter(Task.completion_time.isnot(None))
        query = query.filter(Task.exit_status != 0)

    # Apply group filter
    if group is not None:
        query = query.filter(Task.group == group)

    # Note: Tag filtering would require JSON operations specific to the database dialect
    # For now, we filter in Python for simplicity
    tasks = query.limit(limit * 2 if tag_filter else limit).all()

    # Apply tag filter in Python
    if tag_filter:
        filtered = []
        for task in tasks:
            if all(task.tag.get(k) == v for k, v in tag_filter.items()):
                filtered.append(task)
                if len(filtered) >= limit:
                    break
        tasks = filtered
    else:
        tasks = tasks[:limit]

    return {
        "count": len(tasks),
        "tasks": [task.to_json() for task in tasks],
    }


@mcp.tool
def get_task_output(
    task_id: str,
    output_type: str = "stdout"
) -> Dict[str, Any]:
    """
    Get the output (stdout or stderr) of a completed task.

    Args:
        task_id: The unique UUID of the task.
        output_type: Either "stdout" or "stderr".

    Returns:
        The task output content.
    """
    ensuredb()
    task = Task.from_id(task_id)

    if output_type == "stdout":
        filepath = task.outpath
    elif output_type == "stderr":
        filepath = task.errpath
    else:
        raise ValueError(f"output_type must be 'stdout' or 'stderr', got '{output_type}'")

    if not filepath:
        return {
            "task_id": task_id,
            "output_type": output_type,
            "content": None,
            "message": f"No {output_type} file recorded for this task"
        }

    try:
        with open(filepath, 'r') as f:
            content = f.read()
        return {
            "task_id": task_id,
            "output_type": output_type,
            "filepath": filepath,
            "content": content,
        }
    except FileNotFoundError:
        return {
            "task_id": task_id,
            "output_type": output_type,
            "filepath": filepath,
            "content": None,
            "message": f"Output file not found: {filepath}"
        }


# =============================================================================
# Task Management Tools
# =============================================================================

@mcp.tool
def cancel_task(task_id: str) -> Dict[str, str]:
    """
    Cancel a pending task (mark it as cancelled with exit status -1).

    This only works for tasks that have not yet started execution.

    Args:
        task_id: The unique UUID of the task to cancel.

    Returns:
        Status message.
    """
    ensuredb()
    task = Task.from_id(task_id)

    if task.completion_time is not None:
        return {
            "status": "error",
            "message": f"Task {task_id} has already completed with exit status {task.exit_status}"
        }

    if task.start_time is not None:
        return {
            "status": "error",
            "message": f"Task {task_id} is currently running and cannot be cancelled"
        }

    Task.cancel(task_id)
    return {"status": "success", "message": f"Cancelled task {task_id}"}


@mcp.tool
def cancel_tasks(task_ids: List[str]) -> Dict[str, Any]:
    """
    Cancel multiple pending tasks.

    Args:
        task_ids: List of task UUIDs to cancel.

    Returns:
        Summary of cancellation results.
    """
    ensuredb()

    cancelled = []
    skipped = []

    for task_id in task_ids:
        try:
            task = Task.from_id(task_id)
            if task.completion_time is not None or task.start_time is not None:
                skipped.append({"id": task_id, "reason": "already started or completed"})
            else:
                Task.cancel(task_id)
                cancelled.append(task_id)
        except Task.NotFound:
            skipped.append({"id": task_id, "reason": "not found"})

    return {
        "cancelled": cancelled,
        "cancelled_count": len(cancelled),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


@mcp.tool
def revert_task(task_id: str) -> Dict[str, str]:
    """
    Revert a task to unscheduled state (clear execution data).

    This is useful for re-running a task that was interrupted or needs to be reprocessed.

    Args:
        task_id: The unique UUID of the task to revert.

    Returns:
        Status message.
    """
    ensuredb()
    task = Task.from_id(task_id)

    if task.schedule_time is None:
        return {
            "status": "error",
            "message": f"Task {task_id} has not been scheduled yet"
        }

    Task.revert(task_id)
    return {"status": "success", "message": f"Reverted task {task_id} to unscheduled state"}


@mcp.tool
def revert_interrupted_tasks() -> Dict[str, Any]:
    """
    Revert all interrupted tasks (scheduled but not completed) to unscheduled state.

    This is useful after a server crash or interruption to allow tasks to be rescheduled.

    Returns:
        Count of reverted tasks.
    """
    ensuredb()

    count = Task.count_interrupted()
    Task.revert_interrupted()

    return {
        "status": "success",
        "reverted_count": count,
        "message": f"Reverted {count} interrupted tasks"
    }


@mcp.tool
def update_task_tags(
    task_id: str,
    tags: Dict[str, Any],
    merge: bool = True
) -> Dict[str, Any]:
    """
    Update tags on a task.

    Args:
        task_id: The unique UUID of the task.
        tags: Tags to set on the task.
        merge: If True, merge with existing tags. If False, replace all tags.

    Returns:
        Updated task information.
    """
    ensuredb()
    task = Task.from_id(task_id)

    if merge:
        new_tags = {**task.tag, **tags}
    else:
        new_tags = tags

    Task.update(task_id, tag=new_tags)

    # Fetch updated task
    task = Task.from_id(task_id)
    return {
        "status": "success",
        "task_id": task_id,
        "tags": task.tag,
    }


# =============================================================================
# Resources (read-only data)
# =============================================================================

@mcp.resource("hypershell://config")
def resource_config() -> str:
    """Current HyperShell configuration."""
    import json
    return json.dumps(dict(config), indent=2, default=str)


@mcp.resource("hypershell://stats")
def resource_stats() -> str:
    """Current database statistics."""
    import json
    try:
        ensuredb()
        stats = {
            "provider": DATABASE_PROVIDER,
            "dialect": DATABASE_DIALECT,
            "enabled": DATABASE_ENABLED,
            "tasks": {
                "total": Task.count(),
                "remaining": Task.count_remaining(),
                "interrupted": Task.count_interrupted(),
            }
        }
    except Exception as e:
        stats = {
            "error": str(e),
            "enabled": DATABASE_ENABLED,
        }
    return json.dumps(stats, indent=2)


@mcp.resource("hypershell://version")
def resource_version() -> str:
    """HyperShell version information."""
    from hypershell import __version__ as hs_version
    from hypershell_mcp import __version__ as mcp_version
    import json
    return json.dumps({
        "hypershell": hs_version,
        "hypershell_mcp": mcp_version,
    })


# =============================================================================
# Command-Line Interface
# =============================================================================

from importlib.metadata import version as get_version
from platform import python_version, python_implementation

from cmdkit.app import Application
from cmdkit.cli import Interface

try:
    __version__ = get_version('hypershell-mcp')
except Exception:
    __version__ = '0.1.0'


# Default configuration values
DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 8000
DEFAULT_TRANSPORT = 'stdio'

APP_NAME = 'hypershell-mcp'
APP_VERSION = f'HyperShell MCP Server v{__version__} ({python_implementation()} {python_version()})'
APP_USAGE = f"""\
Usage:
  {APP_NAME} [-h] [-v] [-t TRANSPORT] [-H HOST] [-p PORT]

  MCP Server for HyperShell: Distributed task execution for AI agents.\
"""

APP_HELP = f"""\
{APP_USAGE}

  The HyperShell MCP Server exposes HyperShell functionality through the
  Model Context Protocol (MCP), enabling AI agents to submit and manage
  distributed computing tasks.

  Transports:
    stdio     Standard I/O (default) - for local MCP clients
    sse       Server-Sent Events over HTTP - for web-based clients
    http      Streamable HTTP - for production deployments

Options:
  -t, --transport   TRANSPORT   Transport protocol: stdio, sse, http (default: {DEFAULT_TRANSPORT}).
  -H, --host        HOST        Bind address for HTTP/SSE (default: {DEFAULT_HOST}).
  -p, --port        PORT        Port number for HTTP/SSE (default: {DEFAULT_PORT}).
  -v, --version                 Show version and exit.
  -h, --help                    Show this message and exit.

Examples:
  {APP_NAME}                          # Run with stdio transport
  {APP_NAME} -t sse -p 8080           # Run SSE server on port 8080
  {APP_NAME} -t http -H 0.0.0.0       # Run HTTP server on all interfaces\
"""


class MCPServerApp(Application):
    """HyperShell MCP Server application."""

    interface = Interface(APP_NAME, APP_USAGE, APP_HELP)
    interface.add_argument('-v', '--version', action='version', version=APP_VERSION)
    ALLOW_NOARGS = True

    transport: str = DEFAULT_TRANSPORT
    interface.add_argument('-t', '--transport', default=transport,
                           choices=['stdio', 'sse', 'http'])

    host: str = DEFAULT_HOST
    interface.add_argument('-H', '--host', default=host)

    port: int = DEFAULT_PORT
    interface.add_argument('-p', '--port', type=int, default=port)

    def run(self) -> None:
        """Run the MCP server with configured transport."""
        if self.transport == 'stdio':
            mcp.run(transport='stdio')
        elif self.transport == 'sse':
            mcp.run(transport='sse', host=self.host, port=self.port)
        elif self.transport == 'http':
            mcp.run(transport='streamable-http', host=self.host, port=self.port)
