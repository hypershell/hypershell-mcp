# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""HyperShell MCP: a Model Context Protocol server for HyperShell."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('hypershell-mcp')
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = '0.0.0'

__all__ = ['__version__']
