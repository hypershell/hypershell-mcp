# SPDX-FileCopyrightText: 2025 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""HyperShell MCP Server: Enables agentic development with HyperShell."""

from __future__ import annotations
from typing import List

import sys
from importlib.metadata import version as get_version
from platform import python_version, python_implementation

from cmdkit.app import Application
from cmdkit.cli import Interface

from hypershell_mcp.server import mcp, MCPServerApp

__all__ = ['mcp', 'main', 'MCPServerApp', '__version__']

try:
    __version__ = get_version('hypershell-mcp')
except Exception:
    __version__ = '0.1.0'

__website__ = 'https://github.com/hypershell/hypershell-mcp'
__description__ = 'MCP Server for HyperShell: Distributed task execution for AI agents.'


def main(argv: List[str] | None = None) -> int:
    """Entry point for the hypershell-mcp server."""
    return MCPServerApp.main(argv or sys.argv[1:])
