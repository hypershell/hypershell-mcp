# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Grouping failed tasks by how they failed.

A campaign of ten thousand tasks rarely fails in ten thousand ways. Usually there are two or three
causes: a missing module on some nodes, one input that is malformed, a wall-time limit that is too
short. Reading failures one at a time hides that. Here each failed task is reduced to a signature,
its exit status plus the last line it wrote to stderr with the parts that vary masked out, and
tasks with the same signature are counted together.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Final

from hypershell_mcp.backend import explain_exit_status
from hypershell_mcp.models import FailureCluster, FailureReport

__all__ = ['signature', 'build_report']

MASKS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<id>'),
    (re.compile(r'\b0x[0-9a-fA-F]+\b'), '<hex>'),
    (re.compile(r'(?<![\w.])/(?:[\w.+@-]+/)*[\w.+@-]+'), '<path>'),
    (re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b'), '<time>'),
    (re.compile(r'\b[0-9a-fA-F]{12,}\b'), '<hex>'),
    (re.compile(r'\d+(?:\.\d+)?'), '<n>'),
)


def signature(text: str | None) -> str | None:
    """The last non-empty line of `text` with ids, paths, times and numbers masked."""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            break
    else:
        return None
    for pattern, replacement in MASKS:
        line = pattern.sub(replacement, line)
    return re.sub(r'\s+', ' ', line)[:200]


def build_report(failed: int, tasks: list[dict[str, Any]], tails: dict[str, str | None]) -> FailureReport:
    """
    Cluster `tasks` (rows with id, args, exit_status, client_host) by exit status and signature.

    `tails` maps a task id to the end of its stderr (or stdout when stderr was empty), or None when
    its output could not be read.
    """
    members: dict[tuple[int | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        members[(task.get('exit_status'), signature(tails.get(task['id'])))].append(task)

    clusters = []
    for (status, mark), group in sorted(members.items(), key=lambda item: -len(item[1])):
        example = next((task for task in group if tails.get(task['id'])), group[0])
        tail = tails.get(example['id'])
        clusters.append(FailureCluster(
            exit_status=status, meaning=explain_exit_status(status), signature=mark, count=len(group),
            hosts=dict(Counter(str(task.get('client_host') or 'unknown') for task in group).most_common(10)),
            example_ids=[task['id'] for task in group[:5]], example_command=str(example.get('args'))[:500],
            example_stderr='\n'.join(tail.splitlines()[-12:]) if tail else None))
    return FailureReport(failed=failed, examined=len(tasks), clusters=clusters[:25],
                         advice=advise(tasks, clusters))


def advise(tasks: list[dict[str, Any]], clusters: list[FailureCluster]) -> list[str]:
    """Plain observations that follow from the pattern of failures."""
    if not tasks:
        return []
    advice = []
    hosts = Counter(str(task.get('client_host') or 'unknown') for task in tasks)
    host, on_host = hosts.most_common(1)[0]
    if len(hosts) > 1 and on_host / len(tasks) >= 0.8 and host != 'unknown':
        advice.append(f'{on_host} of {len(tasks)} failures were on {host}: suspect that node, not the tasks.')
    statuses = Counter(task.get('exit_status') for task in tasks)
    share = lambda status: statuses.get(status, 0) / len(tasks)  # noqa: E731
    if share(127) >= 0.2:
        advice.append('Many tasks exited 127 (command not found): the clients may lack a module, a PATH '
                      'entry or an activated environment that the submitting shell had.')
    if share(126) >= 0.2:
        advice.append('Many tasks exited 126: the command exists but is not executable there.')
    if share(137) >= 0.2 or share(-9) >= 0.2:
        advice.append('Many tasks were killed by SIGKILL, which is what running out of memory looks like. '
                      'Declare memory per task so clients do not over-commit.')
    if share(-2) >= 0.2:
        advice.append('Many tasks were interrupted, which is how a wall-time limit ends a task. Raise the '
                      'timeout, or split the work.')
    if share(-1002) > 0:
        advice.append('Some tasks asked for more cores or memory than any client offers; they can never '
                      'start as declared.')
    if share(-1001) > 0:
        advice.append("Some tasks could not be expanded by the client's command template; check the "
                      'template against those commands.')
    if len(clusters) == 1 and len(tasks) > 1:
        advice.append('Every failure has the same signature: one cause. Fix it, then retry_tasks.')
    elif clusters and clusters[0].count / len(tasks) >= 0.7 and len(clusters) > 1:
        advice.append(f'One signature accounts for {clusters[0].count} of {len(tasks)} failures; start there.')
    return advice
