# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Submit-time command templates, textual patterns only.

HyperShell's template engine can run a shell command (``{% cmd %}``) or evaluate Python
(``{= expr =}``) for each input. On a client, under the run tier, that is a feature. At submit time
in this server it would execute inside the server process for a caller who may only add tasks to a
queue. So caller-supplied templates are never handed to that engine. This module expands the
patterns that only rearrange text, with the same meanings HyperShell gives them, and nothing else:
there is no evaluation here to get past.

===========  ==========================================  ========================
Pattern      Expands to                                  For ``data/run1/a.fastq``
===========  ==========================================  ========================
``{}``       the whole input                             ``data/run1/a.fastq``
``{/}``      basename                                    ``a.fastq``
``{/-}``     basename without extension                  ``a``
``{-}``      path without extension                      ``data/run1/a``
``{.}``      directory                                   ``data/run1``
``{..}``     directory of the directory                  ``data``
``{+}``      extension                                   ``.fastq``
``{++}``     extension without the dot                   ``fastq``
``{[0]}``    one whitespace-separated word of the input
``{[1:3]}``  a slice of the words, joined by spaces
===========  ==========================================  ========================

Any other ``{...}`` is ordinary text and is left exactly as written, so a command such as
``awk '{print $1}' {}`` works. The two code patterns are refused outright, with or without padding,
because silently storing them as text would surprise a user who expected HyperShell's behaviour.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Final

__all__ = ['compile_template', 'TemplateError', 'PATTERNS']

PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r'\{(.*?)\}')
SLICE: Final[re.Pattern[str]] = re.compile(r'\[\s*(-?\d+)?\s*(:\s*(-?\d+)?\s*)?(:\s*(-?\d+)?\s*)?\]')

PATTERNS: Final[dict[str, Callable[[str], str]]] = {
    '': lambda text: text,
    '.': os.path.dirname,
    '..': lambda text: os.path.dirname(os.path.dirname(text)),
    '/': os.path.basename,
    '/-': lambda text: os.path.splitext(os.path.basename(text))[0],
    '-': lambda text: os.path.splitext(text)[0],
    '+': lambda text: os.path.splitext(text)[1],
    '++': lambda text: os.path.splitext(text)[1].strip('.'),
}


class TemplateError(Exception):
    """The template cannot be used, or does not fit an input; the message says why."""


def word_picker(key: str) -> Callable[[str], str] | None:
    """An expander for ``[i]`` or ``[a:b:c]`` over the whitespace-separated words of the input."""
    match = SLICE.fullmatch(key)
    if match is None:
        return None
    first, second, stop, third, step = match.groups()
    if second is None and third is None:
        if first is None:
            return None
        index = int(first)

        def pick(text: str) -> str:
            words = text.split()
            try:
                return words[index]
            except IndexError:
                raise TemplateError(f"'{{{key}}}' asks for word {index} but {text!r} has {len(words)}") from None

        return pick
    if step is not None and int(step) == 0:
        raise TemplateError(f"'{{{key}}}' has a step of zero")
    chosen = slice(int(first) if first else None, int(stop) if stop else None, int(step) if step else None)
    return lambda text: ' '.join(text.split()[chosen])


def compile_template(template: str) -> Callable[[str], str]:
    """
    Turn a template into a function from an input to a command.

    Raises `TemplateError` for a template that uses a shell or Python pattern.
    """
    pieces: list[str | Callable[[str], str]] = []
    position = 0
    for match in PLACEHOLDER.finditer(template):
        key = match.group(1).strip()
        expander = PATTERNS.get(key) or word_picker(key)
        if expander is None:
            if key[:1] in ('%', '='):
                kind = 'shell' if key[0] == '%' else 'Python'
                raise TemplateError(f"the submit template may not use the {kind} pattern '{match.group(0)}': it "
                                    'would run on the server. A cluster can apply such a template when it '
                                    'runs the tasks (start_cluster, options.template)')
            continue  # not a pattern: ordinary text, kept as written
        pieces.append(template[position:match.start()])
        pieces.append(expander)
        position = match.end()
    pieces.append(template[position:])
    if not any(callable(piece) for piece in pieces):
        raise TemplateError(f"the template {template!r} has no pattern to put the input in, such as '{{}}'")

    def expand(text: str) -> str:
        return ''.join(piece(text) if callable(piece) else piece for piece in pieces)

    return expand
