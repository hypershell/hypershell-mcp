# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
The tools, one module per tier.

Every tool carries exactly one tier as a tag (see `hypershell_mcp.auth.Tier`). The tag is what
authorization and the ``--disable`` option act on, and `tool` below is the only way tools are
registered, so no tool can be added without one.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from fastmcp import FastMCP
from mcp_types import ToolAnnotations

from hypershell_mcp.app import App
from hypershell_mcp.auth import Tier
from hypershell_mcp.guard import guard

__all__ = ['tool', 'register_all']

F = TypeVar('F', bound=Callable[..., Any])


def tool(mcp: FastMCP, app: App, tier: Tier, title: str, *, destructive: bool = False,
         idempotent: bool = False, open_world: bool = False, **options: Any) -> Callable[[F], F]:
    """
    Register a function as a tool of `tier`.

    The hints tell a client how careful to be: a read-tier tool changes nothing; `destructive`
    marks one whose effect cannot be undone from here; `open_world` marks one that runs commands
    and so can reach anything the host can.
    """
    hints = ToolAnnotations(title=title, read_only_hint=tier is Tier.READ, destructive_hint=destructive,
                            idempotent_hint=idempotent or tier is Tier.READ, open_world_hint=open_world)

    def decorate(function: F) -> F:
        app.tool_tiers[options.get('name') or function.__name__] = tier
        mcp.tool(title=title, tags={tier.value}, annotations=hints, **options)(guard(function))
        return function

    return decorate


def register_all(mcp: FastMCP, app: App) -> None:
    """Register every tool with the server."""
    from hypershell_mcp.tools import admin, read, run, write
    for module in (read, write, run, admin):
        module.register(mcp, app)
