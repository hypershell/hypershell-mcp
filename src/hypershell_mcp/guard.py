# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Telling expected failures from unexpected ones.

The server masks the details of unexpected errors, so a bug never leaks a path or a query to a
remote caller. Expected failures are the opposite: "no task with that id" or "the template failed
on this input" is the answer, and the caller needs to read it. `guard` wraps a tool or resource
function so that the expected kinds are re-raised as the error type FastMCP passes through.
"""

from __future__ import annotations

import functools
import inspect
import typing
from typing import Any, Callable, TypeVar

from fastmcp.exceptions import ResourceError, ToolError

from hypershell_mcp.backend import BackendError
from hypershell_mcp.clusters import ClusterError

__all__ = ['guard', 'EXPECTED']

F = TypeVar('F', bound=Callable[..., Any])
EXPECTED = (BackendError, ClusterError)


def guard(function: F, *, resource: bool = False) -> F:
    """
    Wrap `function` so that an expected failure reaches the caller with its message intact.

    The wrapper carries the original's signature with every annotation already evaluated, because
    FastMCP builds a tool's schema from the signature and would otherwise look the annotation
    names up in this module.
    """
    shown = ResourceError if resource else ToolError

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await function(*args, **kwargs)
            except EXPECTED as failure:
                raise shown(str(failure)) from None
    else:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except EXPECTED as failure:
                raise shown(str(failure)) from None

    hints = typing.get_type_hints(function, include_extras=True)
    signature = inspect.signature(function)
    wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[parameter.replace(annotation=hints.get(name, parameter.annotation))
                    for name, parameter in signature.parameters.items()],
        return_annotation=hints.get('return', signature.return_annotation))
    wrapper.__annotations__ = hints
    del wrapper.__wrapped__  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]
