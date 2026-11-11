# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Reading a task's captured output.

A client started with ``--capture`` writes each task's stdout and stderr to files and records their
paths on the task. Those paths are data in the database, so they are not trusted: a file is read
only when it resolves to somewhere inside a directory this server was told holds task output. What
comes back is bounded in bytes and lines, because the reader is usually a model's context window.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Final, Literal

import regex

from hypershell_mcp.models import OutputChunk

__all__ = ['read_output', 'tail_text', 'SCAN_LIMIT']

SCAN_LIMIT: Final[int] = 64 * 1024 * 1024
"""Most bytes searched for a pattern in one file."""
LINE_LIMIT: Final[int] = 65_536
"""Longest stretch treated as one line; a file with no newlines is searched in pieces this size."""
PATTERN_LIMIT: Final[int] = 300
"""Longest regular expression a caller may send."""
SEARCH_SECONDS: Final[float] = 5.0
"""Longest one search of one file may take."""


def inside(path: Path, roots: tuple[Path, ...]) -> bool:
    """True when `path`, with symlinks resolved, lies within one of `roots`."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def decode(data: bytes) -> str:
    return data.decode('utf-8', errors='replace')


def read_output(task_id: str, stream: Literal['stdout', 'stderr'], path: str | None, *,
                roots: tuple[Path, ...], status: str, client_host: str | None, local_host: str,
                mode: Literal['tail', 'head'] = 'tail', lines: int = 100, max_bytes: int = 32_768,
                pattern: str | None = None) -> OutputChunk:
    """
    The head or tail of one output file, or the lines in it that match `pattern`.

    Never raises for a missing or unreadable file: the result says why the output is not there,
    which is itself the answer a caller needs (not captured, still pending, on another host).
    """

    def missing(reason: str) -> OutputChunk:
        return OutputChunk(task_id=task_id, stream=stream, available=False, reason=reason)

    if not path:
        if status == 'pending':
            return missing('The task has not started, so it has no output yet.')
        return missing('No output file is recorded for this task: the client that ran it was not '
                       'capturing output (clusters started by this server always capture).')
    file = Path(path)
    if not file.exists():
        if client_host and client_host != local_host:
            return missing(f'The output is on {client_host} at {path}, which this server cannot see. '
                           'It is readable here only on a shared filesystem.')
        return missing(f'The recorded output file {path} no longer exists.')
    if not inside(file, roots):
        allowed = ', '.join(str(root) for root in roots)
        return missing(f'The recorded path {path} is outside the directories this server reads task '
                       f'output from ({allowed}). Add its directory to mcp.outputs to allow it.')
    try:
        size = file.stat().st_size
        if pattern is not None:
            return search(task_id, stream, file, size, pattern, lines, max_bytes)
        with open(file, 'rb') as handle:
            if mode == 'head':
                data = handle.read(max_bytes + 1)
                clipped = len(data) > max_bytes
                data = data[:max_bytes]
                kept = data.splitlines(keepends=True)[:lines]
                truncated = clipped or len(data.splitlines()) > lines or size > len(data)
            else:
                handle.seek(max(0, size - max_bytes))
                data = handle.read(max_bytes)
                pieces = data.splitlines(keepends=True)
                if size > max_bytes and len(pieces) > 1:
                    pieces = pieces[1:]  # the first piece is the end of a line cut by the seek
                kept = pieces[-lines:] if lines else []
                truncated = size > max_bytes or len(pieces) > lines
    except regex.error as error:
        return missing(f'The pattern is not a valid regular expression: {error}')
    except TimeoutError:
        return missing(f'The pattern took longer than {SEARCH_SECONDS:.0f} s on this file and was abandoned. '
                       'Simplify it: overlapping alternatives such as (a|aa)+ are the usual cause.')
    except OSError as error:
        return missing(f'Cannot read {path}: {error.strerror}')
    return OutputChunk(task_id=task_id, stream=stream, available=True, text=decode(b''.join(kept)),
                       size=size, lines_returned=len(kept), truncated=truncated)


def search(task_id: str, stream: Literal['stdout', 'stderr'], file: Path, size: int, pattern: str,
           lines: int, max_bytes: int) -> OutputChunk:
    """
    Lines matching `pattern`, numbered, up to `lines` of them and `max_bytes` of text.

    The search is bounded three ways, since the pattern and the file both come from outside: the
    pattern's length, the bytes scanned, and the time spent. The `regex` module is used because it
    can abandon a match that backtracks without end; `re` cannot.
    """
    if len(pattern) > PATTERN_LIMIT:
        raise regex.error(f'it is {len(pattern)} characters; the limit is {PATTERN_LIMIT}')
    expression = regex.compile(pattern)
    deadline = time.monotonic() + SEARCH_SECONDS
    found: list[str] = []
    matches = used = scanned = 0
    with open(file, 'rb') as handle:
        for number, raw in enumerate(iter(lambda: handle.readline(LINE_LIMIT), b''), start=1):
            scanned += len(raw)
            if scanned > SCAN_LIMIT:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            line = decode(raw).rstrip('\n')
            if expression.search(line, timeout=remaining):
                matches += 1
                entry = f'{number}: {line}'
                if len(found) < lines and used + len(entry) <= max_bytes:
                    found.append(entry)
                    used += len(entry) + 1
    return OutputChunk(task_id=task_id, stream=stream, available=True, text='\n'.join(found), size=size,
                       lines_returned=len(found), truncated=matches > len(found) or scanned > SCAN_LIMIT,
                       matches=matches)


def tail_text(path: Path, lines: int, max_bytes: int = 65_536) -> list[str]:
    """The last `lines` lines of a text file, with terminal colour codes removed."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as handle:
            handle.seek(max(0, size - max_bytes))
            text = decode(handle.read(max_bytes))
    except OSError:
        return []
    cleaned = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    return cleaned.splitlines()[-lines:] if lines else []
