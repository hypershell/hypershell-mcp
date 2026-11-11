# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
The HyperShell side of the server: every database read and write the tools make.

HyperShell loads its configuration and builds its database engine when it is imported, and keeps a
thread-local session. So this module imports it lazily, and `Backend.call` runs every operation on
one dedicated thread: one session, one statement at a time, rolled back on error and released after
each call. That is the pattern HyperShell's own threads follow, and it is what Turso requires.

Nothing here knows about MCP. Functions take plain values and return plain values or the models
from `hypershell_mcp.models`; a `BackendError` carries a message fit to show the caller.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import socket
import time
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Final, Iterable, TypeVar

import regex

from hypershell_mcp.templates import TemplateError, compile_template
from hypershell_mcp.models import (STATES, DatabaseInfo, QueueInfo, StatusCounts, TagIndex,
                                   TaskGroupRow, TaskSelector, TaskSpec, TaskSummary)

__all__ = ['Backend', 'BackendError', 'NotFound', 'lib', 'DEFAULT_FIELDS', 'GROUPABLE', 'BUCKETS',
           'explain_exit_status', 'TEMPLATE_HELP']

log = logging.getLogger('hypershell_mcp.backend')
T = TypeVar('T')

DEFAULT_FIELDS: Final[tuple[str, ...]] = ('id', 'status', 'exit_status', 'args', 'group', 'zone', 'tag',
                                          'attempt', 'submit_time', 'start_time', 'completion_time',
                                          'duration', 'client_host')
GROUPABLE: Final[tuple[str, ...]] = ('status', 'group', 'zone', 'exit_status', 'client_host', 'server_host',
                                     'submit_host', 'attempt', 'part')
BUCKETS: Final[dict[str, tuple[str, str]]] = {
    f'{label}:{unit}': (column, unit)
    for label, column in (('submitted', 'submit_time'), ('started', 'start_time'), ('completed', 'completion_time'))
    for unit in ('minute', 'hour', 'day')}
"""Time buckets a summary can group by, e.g. ``completed:hour``: the column and the unit."""
SQLITE_BUCKET: Final[dict[str, str]] = {'minute': '%Y-%m-%d %H:%M', 'hour': '%Y-%m-%d %H:00', 'day': '%Y-%m-%d'}
POSTGRES_BUCKET: Final[dict[str, str]] = {'minute': 'YYYY-MM-DD HH24:MI', 'hour': 'YYYY-MM-DD HH24:00',
                                          'day': 'YYYY-MM-DD'}
OPERATORS: Final[tuple[str, ...]] = ('==', '!=', '>=', '<=', '!~', '>', '<', '~')
WHERE: Final[re.Pattern[str]] = re.compile(r'^\s*([a-z_]+)\s*(==|!=|>=|<=|!~|>|<|~)\s*(.*?)\s*$')
TAG_KEY: Final[re.Pattern[str]] = re.compile(r'^[A-Za-z0-9_.+-]{1,120}$')
TAG_SCAN: Final[int] = 50_000
"""Most tasks read to list the tag keys of a selection."""
PATTERN_LIMIT: Final[int] = 300
"""Longest regular expression a caller may send."""
MATCH_SECONDS: Final[float] = 0.25
"""Longest one regular-expression match may run before the whole query is abandoned."""
CHUNK: Final[int] = 500
"""Rows per statement when changing many tasks, well under SQLite's bound-variable limit."""

TEMPLATE_HELP: Final[str] = (
    "'{}' is the whole input; '{/}' its basename; '{/-}' the basename without extension; '{.}' its "
    "directory; '{-}' the path without extension; '{+}' the extension; '{[0]}' or '{[1:3]}' "
    'whitespace-separated words')


def check_pattern(pattern: str) -> str:
    """Return `pattern` if it is a usable regular expression of reasonable size, else explain."""
    if len(pattern) > PATTERN_LIMIT:
        raise BackendError(f'the pattern is {len(pattern)} characters; the limit is {PATTERN_LIMIT}')
    try:
        regex.compile(pattern)
    except regex.error as error:
        raise BackendError(f'{pattern!r} is not a valid regular expression: {error}') from None
    return pattern


def bounded_regexp(pattern: str | None, text: str | None) -> bool | None:
    """
    SQLite's REGEXP, with a deadline.

    SQLite calls back into Python for every row, on the one thread all database work shares. A
    pattern that backtracks without end (``(a+)+$`` against a long run of ``a``) would hold that
    thread, and with it every caller. The `regex` module can abandon a match; `re` cannot.
    """
    if pattern is None or text is None:
        return None
    try:
        return regex.search(pattern, text, timeout=MATCH_SECONDS) is not None
    except TimeoutError:
        raise RuntimeError(SLOW_PATTERN) from None


SLOW_PATTERN: Final[str] = 'hypershell-mcp: regular expression abandoned as too slow'


class BackendError(Exception):
    """An expected failure with a message that can be shown to the caller as it is."""


class NotFound(BackendError):
    """No such task, cluster or key."""


@functools.lru_cache(maxsize=1)
def lib() -> SimpleNamespace:
    """
    Import HyperShell once and return the pieces used here.

    HyperShell exits the interpreter when its configuration is unusable, so `SystemExit` is turned
    into a `BackendError`; its own message has already gone to stderr.
    """
    try:
        import hypershell
        import sqlalchemy
        from hypershell.core import config as config_module
        from hypershell.core.platform import default_path
        from hypershell.core.types import parse_bytes
        from hypershell.data import core as dbcore
        from hypershell.data import ensuredb
        from hypershell.data import model
    except SystemExit as exit_:
        raise BackendError(f'HyperShell could not start (exit status {exit_.code}): its configuration '
                           "is not usable. Run 'hs config get database' in the same environment to see why.") from None
    if dbcore.engine.dialect.name == 'sqlite':
        @sqlalchemy.event.listens_for(dbcore.engine, 'connect')
        def install_bounded_regexp(connection: Any, record: Any) -> None:
            if hasattr(connection, 'create_function'):  # Turso's driver may not offer it
                connection.create_function('regexp', 2, bounded_regexp)

        # A connection opened before now has SQLAlchemy's own REGEXP, which has no deadline.
        dbcore.engine.dispose()
    return SimpleNamespace(
        version=hypershell.__version__, sa=sqlalchemy, config=config_module.config,
        config_module=config_module, default_path=default_path,
        parse_bytes=parse_bytes, db=dbcore, Session=dbcore.Session, ensuredb=ensuredb, model=model,
        Task=model.Task, Client=model.Client, Zone=model.Zone, Source=model.Source,
        CANCEL_STATUS=model.CANCEL_STATUS, DEFAULT_ZONE_NAME=model.DEFAULT_ZONE_NAME,
    )


EXIT_MEANINGS: Final[dict[int, str]] = {
    1: 'general error', 2: 'misuse of a shell builtin or bad arguments',
    # The BSD sysexits conventions, which well-behaved tools use to say what kind of failure it was.
    64: 'usage error (sysexits)', 65: 'bad input data (sysexits)', 66: 'cannot open input (sysexits)',
    69: 'a service is unavailable (sysexits)', 70: 'internal software error (sysexits)',
    71: 'operating system error (sysexits)', 73: 'cannot create output file (sysexits)',
    74: 'input/output error (sysexits)',
    75: 'temporary failure (sysexits): the program is asking to be retried',
    77: 'permission denied (sysexits)', 78: 'configuration error (sysexits)',
    126: 'command found but not executable', 127: 'command not found',
    130: 'terminated by Ctrl-C (SIGINT)', 137: 'killed (SIGKILL): often out of memory',
    139: 'segmentation fault (SIGSEGV)', 143: 'terminated (SIGTERM)',
}


def explain_exit_status(status: int | None) -> str:
    """What an exit status usually means, in a few words."""
    if status is None:
        return 'not finished'
    if status == 0:
        return 'succeeded'
    if status == -1:
        return 'cancelled (or killed by SIGHUP, which HyperShell records the same way)'
    if status == -1001:
        return 'the command template could not be expanded for this task'
    if status == -1002:
        return 'the task asked for more cores or memory than the client has'
    if status == -2:
        return 'interrupted (SIGINT): usually the task reached its wall-time limit'
    if status < 0:
        try:
            import signal
            return f'killed by {signal.Signals(-status).name}'
        except ValueError:
            return f'killed by signal {-status}'
    if status in EXIT_MEANINGS:
        return EXIT_MEANINGS[status]
    if status > 128:
        return f'exited {status}: likely killed by signal {status - 128}'
    return f'exited with status {status}'


class Backend:
    """
    The one doorway to HyperShell's database.

    `await backend.call(function, ...)` runs `function` on the backend thread with a clean session.
    The functions themselves are the methods below; they are synchronous and must only ever be run
    through `call`.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hypershell')
        self._ready = False

    async def call(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(self._guarded, function, *args, **kwargs))

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _guarded(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        hs = lib()
        try:
            self._ensure_database(hs)
            return function(*args, **kwargs)
        except BackendError:
            hs.Session.rollback()
            raise
        except hs.model.NotFound as error:
            hs.Session.rollback()
            raise NotFound(str(error) or 'not found') from None
        except Exception as error:
            hs.Session.rollback()
            if SLOW_PATTERN in str(error) or 'user-defined function raised exception' in str(error):
                raise BackendError('The regular expression took too long on some task and was abandoned. '
                                   'Simplify it: overlapping alternatives such as (a|aa)+ are the usual cause.') from None
            raise
        finally:
            hs.Session.remove()

    def _ensure_database(self, hs: SimpleNamespace) -> None:
        """Create or upgrade a SQLite database, and check any other is initialised. Once."""
        if self._ready:
            return
        try:
            hs.ensuredb()
        except Exception as error:
            name = type(error).__name__
            if name == 'ConfigurationError':
                raise BackendError('No database is configured: HyperShell is in its in-memory mode. Set '
                                   'database.file (or HYPERSHELL_DATABASE_FILE) and restart the server.') from None
            if name == 'DatabaseUninitialized':
                raise BackendError('The database has no HyperShell tables yet. Call init_database '
                                   "(admin), or run 'hs initdb'.") from None
            raise
        self._ready = True

    def forget_database(self) -> None:
        """Check the database again on the next call (after an admin operation changed it)."""
        self._ready = False

    # ---------------------------------------------------------------------------------------------
    # Describing the site

    @staticmethod
    def database_info() -> DatabaseInfo:
        hs = lib()
        database = hs.config.database
        provider = str(database.get('provider', 'sqlite'))
        file = database.get('file')
        if file and provider in ('sqlite', 'turso'):
            path = Path(str(file))
            size = path.stat().st_size if path.exists() else None
            # HyperShell rotates main.db into main.1, main.2, ... beside it
            partitions = len([p for p in path.parent.glob(f'{path.stem}.*') if p.suffix[1:].isdigit()])
            return DatabaseInfo(provider=provider, location=str(path), size_bytes=size, partitions=partitions)
        host, name = database.get('host', 'localhost'), database.get('database', '')
        return DatabaseInfo(provider=provider, location=f'{host}/{name}'.rstrip('/'))

    @staticmethod
    def site_paths() -> dict[str, str]:
        hs = lib()
        return {'lib': str(hs.default_path.lib), 'log': str(hs.default_path.log),
                'config': str(hs.default_path.config)}

    # ---------------------------------------------------------------------------------------------
    # Selecting tasks

    @staticmethod
    def status_expression() -> Any:
        """SQL CASE that names a task's state. Mirrors `Task.status_label`, in this server's words."""
        hs = lib()
        Task = hs.Task
        return hs.sa.case(
            (Task.schedule_time.is_(None), 'pending'),
            (Task.completion_time.is_(None), 'running'),
            (Task.exit_status == 0, 'succeeded'),
            (Task.exit_status == hs.CANCEL_STATUS, 'cancelled'),
            else_='failed')

    @staticmethod
    def state_filter(state: str) -> Any:
        hs = lib()
        Task, sa = hs.Task, hs.sa
        if state == 'pending':
            return Task.schedule_time.is_(None)
        if state == 'running':
            return sa.and_(Task.schedule_time.isnot(None), Task.completion_time.is_(None))
        if state == 'succeeded':
            return Task.exit_status == 0
        if state == 'cancelled':
            return sa.and_(Task.completion_time.isnot(None), Task.exit_status == hs.CANCEL_STATUS)
        if state == 'failed':
            return sa.and_(Task.completion_time.isnot(None), Task.exit_status.isnot(None),
                           Task.exit_status.notin_((0, hs.CANCEL_STATUS)))
        if state == 'completed':
            return Task.completion_time.isnot(None)
        if state == 'remaining':
            return Task.completion_time.is_(None)
        raise BackendError(f'unknown status {state!r}')

    @staticmethod
    def tag_filter(key: str, value: Any) -> Any:
        """Match a tag by presence (`value` None) or by value, on SQLite/Turso and PostgreSQL."""
        hs = lib()
        if not TAG_KEY.fullmatch(key):
            raise BackendError(f'invalid tag key {key!r}: use letters, digits and _ . + -')
        Task, sa = hs.Task, hs.sa
        sqlite = hs.db.engine.dialect.name == 'sqlite'
        if value is None:
            if sqlite:
                return sa.text(f'json_extract(task.tag, \'$."{key}"\') is not null')
            return Task.tag[key].isnot(None)
        if sqlite and isinstance(value, bool):
            value = int(value)  # SQLite stores JSON booleans as 0/1
        return Task.tag[key] == sa.type_coerce(value, hs.model.JSON)

    @classmethod
    def where_filter(cls, condition: str) -> Any:
        """Compile one 'FIELD OP VALUE' condition."""
        hs = lib()
        Task, sa = hs.Task, hs.sa
        match = WHERE.match(condition)
        if not match:
            raise BackendError(f"cannot read condition {condition!r}: write FIELD OP VALUE, where OP is "
                               f"one of {' '.join(OPERATORS)}")
        name, operator, raw = match.groups()
        if name == 'tag':
            raise BackendError("filter on tags with selector.tags, not a 'tag' condition")
        if name == 'status':
            raise BackendError("filter on status with selector.status, not a 'status' condition")
        if name not in Task.columns:
            raise BackendError(f"unknown field {name!r}; fields are: {', '.join(c for c in Task.columns if c != 'tag')}")
        column, kind = getattr(Task, name), Task.columns[name]
        if raw.lower() in ('null', 'none'):
            if operator == '==':
                return column.is_(None)
            if operator == '!=':
                return column.isnot(None)
            raise BackendError(f'null can only be compared with == or != (in {condition!r})')
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '\'"':
            raw = raw[1:-1]
        if operator in ('~', '!~'):
            if kind is not str:
                raise BackendError(f'{name} is not text, so it cannot be matched with {operator}')
            expression = column.regexp_match(check_pattern(raw))
            return expression if operator == '~' else sa.not_(expression)
        value = cls.coerce(name, kind, raw)
        if name == 'zone' or kind is bool:
            if operator not in ('==', '!='):
                raise BackendError(f'{name} can only be compared with == or !=')
            if value is None:
                return column.is_(None) if operator == '==' else column.isnot(None)
        if operator == '==':
            return column == value
        if operator == '!=':
            return column != value
        if operator == '>':
            return column > value
        if operator == '>=':
            return column >= value
        if operator == '<':
            return column < value
        return column <= value

    @staticmethod
    def coerce(name: str, kind: type, raw: str) -> Any:
        """Turn the text of a condition's value into the column's type."""
        hs = lib()
        try:
            if name == 'zone':
                return None if raw == hs.DEFAULT_ZONE_NAME else hs.model.zone_id(hs.model.zone_name(raw))
            if kind is bool:
                if raw.lower() in ('true', '1', 'yes'):
                    return True
                if raw.lower() in ('false', '0', 'no'):
                    return False
                raise ValueError('expected true or false')
            if kind is int:
                return int(raw)
            if kind is float:
                return float(raw)
            if kind is datetime:
                return datetime.fromisoformat(raw)
        except ValueError as error:
            raise BackendError(f'cannot read {raw!r} as a value for {name}: {error}') from None
        return raw

    @classmethod
    def filters(cls, selector: TaskSelector | None) -> list[Any]:
        """The SQL conditions for a selector."""
        hs = lib()
        Task, sa = hs.Task, hs.sa
        if selector is None:
            return []
        found: list[Any] = []
        if selector.ids:
            found.append(Task.id.in_([cls.task_id(value) for value in selector.ids]))
        if selector.batch:
            found.append(cls.tag_filter('batch', selector.batch))
        if selector.status:
            found.append(sa.or_(*[cls.state_filter(state) for state in selector.status]))
        for key, value in (selector.tags or {}).items():
            found.append(cls.tag_filter(key, value))
        if selector.group is not None:
            found.append(Task.group == selector.group)
        if selector.zone:
            zone = cls.coerce('zone', str, selector.zone)
            found.append(Task.zone.is_(None) if zone is None else Task.zone == zone)
        for condition in selector.where or []:
            found.append(cls.where_filter(condition))
        if selector.superseded is not None:
            found.append(Task.retried.is_(True) if selector.superseded else sa.or_(Task.retried.is_(False),
                                                                                  Task.retried.is_(None)))
        return found

    @staticmethod
    def task_id(value: str) -> str:
        """A canonical task id, or an error that says what was wrong with it."""
        try:
            return str(uuidlib.UUID(str(value).strip()))
        except ValueError:
            raise BackendError(f'{value!r} is not a task id (a UUID such as '
                               '0193f1c2-7d2a-7c7e-9d3b-2f6a1b0c4d5e)') from None

    # ---------------------------------------------------------------------------------------------
    # Reading

    @classmethod
    def count(cls, selector: TaskSelector | None = None) -> StatusCounts:
        hs = lib()
        status = cls.status_expression()
        query = hs.Session.query(status, hs.sa.func.count(hs.Task.id)).filter(*cls.filters(selector)).group_by(status)
        counts = {state: 0 for state in STATES}
        counts.update({state: number for state, number in query.all()})
        return StatusCounts(**counts, total=sum(counts.values()))

    @classmethod
    def zone_names(cls) -> dict[str | None, str]:
        hs = lib()
        names: dict[str | None, str] = {None: hs.DEFAULT_ZONE_NAME}
        names.update({zone.id: zone.name for zone in hs.Zone.query().all()})
        return names

    @classmethod
    def select(cls, selector: TaskSelector | None, fields: Iterable[str] | None, order_by: str,
               descending: bool, limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
        """A page of tasks as dictionaries of `fields`, and the total number matching."""
        hs = lib()
        Task = hs.Task
        fields = list(fields or DEFAULT_FIELDS)
        unknown = [name for name in fields if name not in Task.columns and name != 'status']
        if unknown:
            raise BackendError(f"unknown field(s) {', '.join(unknown)}; fields are: status, {', '.join(Task.columns)}")
        if order_by not in Task.columns or order_by == 'tag':
            raise BackendError(f'cannot order by {order_by!r}; choose a task field other than tag')
        conditions = cls.filters(selector)
        total = hs.Session.query(hs.sa.func.count(Task.id)).filter(*conditions).scalar() or 0
        columns = [cls.status_expression().label('status') if name == 'status' else getattr(Task, name)
                   for name in fields]
        order = getattr(Task, order_by)
        query = (hs.Session.query(*columns).filter(*conditions)
                 .order_by(order.desc() if descending else order.asc(), Task.id.asc())
                 .offset(offset).limit(limit))
        zones = cls.zone_names() if 'zone' in fields else {}
        return total, [cls.render(dict(zip(fields, row)), zones) for row in query.all()]

    @staticmethod
    def render(row: dict[str, Any], zones: dict[str | None, str]) -> dict[str, Any]:
        """Make a row JSON-ready: ISO timestamps and zone names."""
        for name, value in row.items():
            if isinstance(value, datetime):
                row[name] = value.isoformat(sep=' ', timespec='seconds')
        if 'zone' in row:
            row['zone'] = zones.get(row['zone'], row['zone'])
        return row

    @classmethod
    def get(cls, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """One task in full, with the retry chain it belongs to (oldest first, itself included)."""
        hs = lib()
        Task = hs.Task
        zones = cls.zone_names()

        def describe(task: Any) -> dict[str, Any]:
            row = cls.render(task.to_dict(), zones)
            row['status'] = cls.state_of(task)
            row['status_detail'] = explain_exit_status(task.exit_status)
            return row

        task = Task.from_id(cls.task_id(task_id), caching=False)
        chain, seen = [task], {task.id}
        while chain[0].previous_id and chain[0].previous_id not in seen and len(chain) < 50:
            previous = Task.query().filter(Task.id == chain[0].previous_id).first()
            if previous is None:
                break
            chain.insert(0, previous)
            seen.add(previous.id)
        while chain[-1].next_id and chain[-1].next_id not in seen and len(chain) < 50:
            following = Task.query().filter(Task.id == chain[-1].next_id).first()
            if following is None:
                break
            chain.append(following)
            seen.add(following.id)
        attempts = [] if len(chain) == 1 else [
            {key: describe(item)[key] for key in ('id', 'attempt', 'status', 'exit_status', 'status_detail',
                                                  'client_host', 'start_time', 'completion_time', 'duration')}
            for item in chain]
        return describe(task), attempts

    @staticmethod
    def state_of(task: Any) -> str:
        hs = lib()
        if task.schedule_time is None:
            return 'pending'
        if task.completion_time is None:
            return 'running'
        if task.exit_status == 0:
            return 'succeeded'
        if task.exit_status == hs.CANCEL_STATUS:
            return 'cancelled'
        return 'failed'

    @classmethod
    def output_paths(cls, task_id: str) -> dict[str, Any]:
        """Where a task's captured output is, and enough about the task to explain its absence."""
        hs = lib()
        task = hs.Task.from_id(cls.task_id(task_id), caching=False)
        return {'id': task.id, 'stdout': task.outpath, 'stderr': task.errpath, 'status': cls.state_of(task),
                'client_host': task.client_host, 'exit_status': task.exit_status}

    @classmethod
    def summarize(cls, selector: TaskSelector | None, group_by: list[str], limit: int) -> TaskSummary:
        """Count tasks, and their durations, by any mix of fields and tags."""
        hs = lib()
        Task, sa, func = hs.Task, hs.sa, hs.sa.func
        sqlite = hs.db.engine.dialect.name == 'sqlite'
        keys: list[Any] = []
        for name in group_by:
            if name == 'status':
                keys.append(cls.status_expression())
            elif name in GROUPABLE:
                keys.append(getattr(Task, name))
            elif name.startswith('tag:') and TAG_KEY.fullmatch(name[4:]):
                key = name[4:]
                keys.append(func.json_extract(Task.tag, f'$."{key}"') if sqlite else Task.tag[key].astext)
            elif name in BUCKETS:
                column, unit = BUCKETS[name]
                moment = getattr(Task, column)
                keys.append(func.strftime(SQLITE_BUCKET[unit], moment) if sqlite
                            else func.to_char(func.date_trunc(unit, moment), POSTGRES_BUCKET[unit]))
            else:
                raise BackendError(f"cannot group by {name!r}; choose from {', '.join(GROUPABLE)}, tag:KEY, or "
                                   f"a time bucket such as completed:hour ({', '.join(BUCKETS)})")
        labelled = [key.label(f'key_{index}') for index, key in enumerate(keys)]
        conditions = cls.filters(selector)
        total = hs.Session.query(func.count(Task.id)).filter(*conditions).scalar() or 0
        query = (hs.Session.query(*labelled, func.count(Task.id), func.avg(Task.duration), func.min(Task.duration),
                                  func.max(Task.duration), func.sum(Task.duration), func.avg(Task.waited))
                 .filter(*conditions))
        if labelled:
            # Largest groups first, unless the rows form a timeline, which reads in time order.
            timeline = any(name in BUCKETS for name in group_by)
            query = query.group_by(*labelled).order_by(*(labelled if timeline else
                                                         (func.count(Task.id).desc(), *labelled)))
        records = query.limit(limit + 1).all()
        zones = cls.zone_names() if 'zone' in group_by else {}
        rows = []
        for record in records[:limit]:
            values, (count, mean, low, high, total_duration, waited) = record[:len(keys)], record[len(keys):]
            key = dict(zip(group_by, values))
            if 'zone' in key:
                key['zone'] = zones.get(key['zone'], key['zone'])
            rows.append(TaskGroupRow(
                key=key, count=count,
                duration_mean=round(float(mean), 3) if mean is not None else None,
                duration_min=float(low) if low is not None else None,
                duration_max=float(high) if high is not None else None,
                duration_total=float(total_duration) if total_duration is not None else None,
                waited_mean=round(float(waited), 3) if waited is not None else None))
        return TaskSummary(total=total, group_by=group_by, rows=rows, truncated=len(records) > limit)

    @classmethod
    def clients_connected(cls, stale_before: float | None = None) -> int:
        """
        Clients the most recent server records as connected.

        A server that is killed never marks its clients disconnected, so the record outlives them.
        When the caller knows the server ended at `stale_before` (a timestamp), clients that
        connected before then have no server and are not counted.
        """
        hs = lib()
        Client = hs.Client
        try:
            if stale_before is None:
                return hs.Client.count_connected()
            server = hs.Task.latest_server()
            if server is None:
                return 0
            rows = (hs.Session.query(Client.connected_at)
                    .filter(Client.server_id == server, Client.disconnected_at.is_(None))
                    .filter(hs.sa.or_(Client.relay.is_(None), Client.relay != Client.id)).all())
            return sum(1 for (connected,) in rows if connected is not None and connected.timestamp() > stale_before)
        except Exception as error:  # an older database without the client table's newer columns
            log.debug('Could not count connected clients: %s', error)
            hs.Session.rollback()
            return 0

    @classmethod
    def queue(cls, stale_before: float | None = None) -> QueueInfo:
        """What is left to run, where, and how fast it is going."""
        hs = lib()
        Task, func = hs.Task, hs.sa.func
        remaining = Task.completion_time.is_(None)
        groups = dict(hs.Session.query(Task.group, func.count(Task.id)).filter(remaining)
                      .group_by(Task.group).order_by(Task.group).limit(50).all())
        zones = cls.zone_names()
        by_zone = {zones.get(zone, str(zone)): number for zone, number in
                   hs.Session.query(Task.zone, func.count(Task.id)).filter(remaining).group_by(Task.zone).all()}
        superseded = hs.Session.query(func.count(Task.id)).filter(Task.retried.is_(True)).scalar() or 0
        rate = eta = None
        try:
            rate = Task.effective_rate()
            if rate:
                eta = sum(groups.values()) / rate
        except (ZeroDivisionError, TypeError):
            pass  # no interval to measure yet: every finished task shares one timestamp
        connected = cls.clients_connected(stale_before)
        return QueueInfo(current_group=min(groups) if groups else None,
                         groups_remaining={str(group): number for group, number in groups.items()},
                         zones_remaining=by_zone, clients_connected=connected,
                         tasks_per_second=round(rate, 4) if rate else None,
                         seconds_to_completion=round(eta, 1) if eta else None,
                         superseded_attempts=superseded)

    @classmethod
    def tags(cls, key: str | None, selector: TaskSelector | None, limit: int) -> TagIndex:
        """Distinct tag keys, or the values of one key with how many tasks carry each."""
        hs = lib()
        Task, func = hs.Task, hs.sa.func
        sqlite = hs.db.engine.dialect.name == 'sqlite'
        conditions = cls.filters(selector)
        if key is None:
            if sqlite:
                statement = hs.sa.text('select distinct j.key from task, json_each(task.tag) as j order by 1')
                if conditions:
                    # json_each cannot be combined with ORM filters cheaply; fall back to scanning
                    scanned = hs.Session.query(Task.tag).filter(*conditions).limit(TAG_SCAN + 1).all()
                    keys = sorted({name for (tag,) in scanned[:TAG_SCAN] for name in (tag or {})})
                    return TagIndex(keys=keys[:limit], truncated=len(keys) > limit or len(scanned) > TAG_SCAN)
                keys = [row[0] for row in hs.Session.execute(statement)]
            else:
                keys = [row[0] for row in hs.Session.query(func.jsonb_object_keys(Task.tag)).filter(*conditions)
                        .distinct().all()]
                keys.sort()
            return TagIndex(keys=keys[:limit], truncated=len(keys) > limit)
        if not TAG_KEY.fullmatch(key):
            raise BackendError(f'invalid tag key {key!r}')
        value = func.json_extract(Task.tag, f'$."{key}"') if sqlite else Task.tag[key].astext
        records = (hs.Session.query(value, func.count(Task.id)).filter(*conditions, cls.tag_filter(key, None))
                   .group_by(value).order_by(func.count(Task.id).desc()).limit(limit + 1).all())
        return TagIndex(key=key, values={str(name): number for name, number in records[:limit]},
                        truncated=len(records) > limit)

    # ---------------------------------------------------------------------------------------------
    # Writing

    @classmethod
    def submit(cls, tasks: list[str | TaskSpec], *, template: str | None, tags: dict[str, Any] | None,
               group: int | None, zone: str | None, cores: int | None, memory: str | None,
               timeout: int | None, batch: str | None, submitter: str | None,
               dry_run: bool = False) -> dict[str, Any]:
        """
        Add tasks to the database and return their ids.

        Commands are stored verbatim: nothing after a ``#`` is stripped and no inline tag comment
        is read, because per-task options have their own fields here. The submit-time template is
        expanded by `hypershell_mcp.templates`, which only rearranges text; HyperShell's own engine,
        which can run shell and Python, never sees a caller's template.
        """
        hs = lib()
        Task = hs.Task
        if not tasks:
            raise BackendError('nothing to submit: give at least one command')
        expand: Callable[[str], str] = lambda text: text
        if template and template != '{}':
            try:
                compiled = compile_template(template)
            except TemplateError as error:
                raise BackendError(f'{error}. Patterns: {TEMPLATE_HELP}') from None

            def expand(text: str) -> str:
                try:
                    return compiled(text)
                except TemplateError as error:
                    raise BackendError(str(error)) from None

        batch = batch or cls.new_batch_id()
        if not TAG_KEY.fullmatch(batch):
            raise BackendError(f'invalid batch id {batch!r}: use letters, digits and _ . + -')
        cls.check_tags(tags)
        # Set after the caller's tags, and again after each task's: who submitted a task and
        # which batch it belongs to are this server's to say.
        stamped = {'batch': batch}
        if submitter:
            stamped['submitter'] = re.sub(r'[^A-Za-z0-9_.+-]', '_', submitter)[:120]
        shared = {**(tags or {}), **stamped}
        if group is None:
            group = Task.current_group().value
        zone_id = None
        if zone and zone != hs.DEFAULT_ZONE_NAME:
            name = hs.model.zone_name(zone)
            zone_id = hs.model.zone_id(name) if dry_run else hs.Zone.ensure(name)
        source = hs.Source.reserved(hs.model.DIRECT_SOURCE_ID).id
        shared_memory = cls.parse_memory(memory)

        new = []
        for item in tasks:
            spec = item if isinstance(item, TaskSpec) else TaskSpec(command=item)
            cls.check_tags(spec.tags)
            command = expand(spec.command.strip())
            if not command:
                raise BackendError('a command is empty')
            if '\n' in command or '\x00' in command:
                raise BackendError('a command must be one line; join steps with && or submit a script path')
            try:
                new.append(Task.new(
                    args=command, raw_args=spec.command.strip(), parse_inline=False,
                    tag={**shared, **{k: v for k, v in (spec.tags or {}).items() if v is not None}, **stamped},
                    group=spec.group if spec.group is not None else group, source=source, zone=zone_id,
                    cores=spec.cores or cores, memory=cls.parse_memory(spec.memory) or shared_memory,
                    timeout=spec.timeout or timeout))
            except BackendError:
                raise
            except Exception as error:  # tag validation and friends raise assorted HyperShell errors
                raise BackendError(f'cannot submit {spec.command!r}: {error}') from None
        groups = {task.group for task in new}
        result = {'batch': shared['batch'], 'submitted': len(new), 'ids': [task.id for task in new],
                  'group': groups.pop() if len(groups) == 1 else None,
                  'zone': zone if zone_id else hs.DEFAULT_ZONE_NAME, 'dry_run': dry_run,
                  'preview': [task.args for task in new[:10]] if dry_run else []}
        if dry_run:
            return {**result, 'batch': None, 'submitted': 0, 'ids': [], 'would_submit': len(new)}
        for start in range(0, len(new), CHUNK):
            Task.add_all(new[start:start + CHUNK])
        return {**result, 'would_submit': None}

    OPTION_TAGS: Final[frozenset[str]] = frozenset({'group', 'cores', 'memory', 'timeout'})
    """Tag names HyperShell lifts into task columns, untyped. Never accepted as tags here."""

    STAMPED_TAGS: Final[frozenset[str]] = frozenset({'batch', 'submitter'})
    """Tags this server sets on what it submits."""

    @classmethod
    def check_tags(cls, tags: dict[str, Any] | None, *, changing: bool = False) -> None:
        """
        Refuse tag names that are not the caller's to set.

        HyperShell moves tags named group, cores, memory and timeout into the task's columns
        without checking their type, so `{"group": "control"}` would store a string where the
        scheduler expects a number and break every query that sorts on it. Those are options, with
        their own typed fields. `submitter` records who submitted a task and is never caller-set;
        `batch` is set through the batch argument at submit, and may be changed afterwards.
        """
        names = set(tags or {})
        for name in names:
            if not TAG_KEY.fullmatch(str(name)):
                raise BackendError(f'invalid tag key {name!r}: use letters, digits and _ . + -')
        options = sorted(names & cls.OPTION_TAGS)
        if options:
            raise BackendError(f"{', '.join(options)} cannot be used as a tag name: HyperShell reads it as a "
                               'task option. Use the argument of that name instead.')
        stamped = sorted(names & ({'submitter'} if changing else cls.STAMPED_TAGS))
        if stamped:
            raise BackendError(f"{', '.join(stamped)} is set by the server and cannot be given as a tag"
                               + ('' if changing else '; to choose the batch, use the batch argument'))

    @staticmethod
    def parse_memory(text: str | None) -> int | None:
        if not text:
            return None
        try:
            return lib().parse_bytes(str(text))
        except ValueError:
            raise BackendError(f"cannot read {text!r} as memory: write e.g. '512MB' or '4GB'") from None

    @staticmethod
    def new_batch_id() -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuidlib.uuid4().hex[:4]}"

    @classmethod
    def matching_ids(cls, selector: TaskSelector, *extra: Any) -> list[str]:
        hs = lib()
        return [row[0] for row in hs.Session.query(hs.Task.id).filter(*cls.filters(selector), *extra)
                .order_by(hs.Task.id).all()]

    @classmethod
    def change(cls, action: str, selector: TaskSelector, *, dry_run: bool, everything: bool = False,
               set_tags: dict[str, Any] | None = None, remove_tags: list[str] | None = None) -> dict[str, Any]:
        """
        Cancel, retry, tag or delete the tasks a selector matches.

        Each action only touches tasks it is safe for: `cancel` unfinished tasks, `retry` finished
        tasks that no retry has replaced, `delete` tasks that are not running. The rest are counted
        as skipped. Returns the counts and a sample of ids; with `dry_run` nothing is written.
        """
        hs = lib()
        Task, sa = hs.Task, hs.sa
        if selector.is_empty() and not everything:
            raise BackendError(f'refusing to {action} every task: give a selector, or pass everything=true '
                               'to mean the whole database')
        running = cls.state_filter('running')
        note = None
        if action == 'cancel':
            eligible = Task.completion_time.is_(None)
            note = ('Only unfinished tasks are cancelled. A task that was already running keeps running '
                    'on its client; its result is discarded when it returns.')
        elif action == 'retry':
            eligible = sa.and_(Task.completion_time.isnot(None), sa.or_(Task.retried.is_(False), Task.retried.is_(None)))
            note = ('Only finished tasks are re-queued; running tasks and attempts already replaced by an '
                    'automatic retry are skipped. A re-queued task forgets its previous result and output.')
        elif action == 'delete':
            eligible = sa.not_(running)
            note = 'Running tasks are not deleted; cancel them first, or wait for them.'
        elif action == 'tag':
            eligible = sa.true()
            if not set_tags and not remove_tags:
                raise BackendError('nothing to change: give tags to set or tag keys to remove')
            cls.check_tags(set_tags, changing=True)
            cls.check_tags(dict.fromkeys(remove_tags or []), changing=True)
        else:
            raise BackendError(f'unknown action {action!r}')

        matched = hs.Session.query(sa.func.count(Task.id)).filter(*cls.filters(selector)).scalar() or 0
        ids = cls.matching_ids(selector, eligible)
        result = {'action': action, 'dry_run': dry_run, 'matched': matched, 'changed': 0,
                  'skipped': matched - len(ids), 'sample_ids': ids[:20], 'note': note}
        if dry_run or not ids:
            return result

        changed = 0
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            if action == 'cancel':
                Task.cancel_all(chunk)
                changed += len(chunk)
            elif action == 'retry':
                changed += len(Task.revert_all(chunk))
            elif action == 'delete':
                changed += hs.Session.query(Task).filter(Task.id.in_(chunk)).delete(synchronize_session=False)
                hs.Session.commit()
            else:
                rows = hs.Session.query(Task.id, Task.tag).filter(Task.id.in_(chunk)).all()
                updates = []
                for task_id, tag in rows:
                    merged = {**(tag or {}), **{k: v for k, v in (set_tags or {}).items() if v is not None}}
                    for key in remove_tags or []:
                        merged.pop(key, None)
                    if merged != (tag or {}):
                        Task.ensure_valid_tag(merged)
                        updates.append({'id': task_id, 'tag': merged})
                if updates:
                    Task.update_all(updates)
                changed += len(updates)
        result['changed'] = changed
        result['skipped'] = matched - changed
        return result

    # ---------------------------------------------------------------------------------------------
    # Configuration

    SECRET_WORDS: Final[tuple[str, ...]] = ('auth', 'password', 'secret', 'token', 'key', 'passwd', 'credential')
    PLACEHOLDERS: Final[tuple[Any, ...]] = (None, '', '<none>', '<auto>', '<not-secure>')
    MASK: Final[str] = '********'

    @classmethod
    def redact(cls, value: Any, name: str = '', *, exported: bool = False) -> Any:
        """
        A copy of a configuration value with anything that could be a secret masked.

        Masked: values whose name suggests a secret; everything under ``export`` (those become
        environment variables for tasks, which is where API keys live); and the credentials and
        query string of anything that looks like a URL, whatever it is called.
        """
        lowered = name.lower()
        if isinstance(value, dict):
            return {key: cls.redact(item, str(key), exported=exported or lowered == 'export')
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.redact(item, name, exported=exported) for item in value]
        if value in cls.PLACEHOLDERS or isinstance(value, bool):
            return value
        if exported or (any(word in lowered for word in cls.SECRET_WORDS) and lowered not in ('keep', 'monkey')):
            return cls.MASK
        if isinstance(value, str) and '://' in value:
            return cls.redact_url(value)
        return value

    @classmethod
    def redact_url(cls, text: str) -> str:
        """A URL without its userinfo or query string, which are where credentials ride."""
        scheme, _, rest = text.partition('://')
        rest, question, _ = rest.partition('?')
        authority, slash, path = rest.partition('/')
        if '@' in rest:
            # The password may itself contain '/', so split on the last '@' of the whole remainder.
            credentials, _, after = rest.rpartition('@')
            authority, slash, path = after.partition('/')
            authority = f'{cls.MASK}@{authority}'
        return f'{scheme}://{authority}{slash}{path}' + (f'?{cls.MASK}' if question else '')

    @classmethod
    def config_get(cls, key: str | None) -> tuple[Any, str | None]:
        """A configuration value (or section, or everything), redacted, and the layer that set it."""
        hs = lib()
        value: Any = dict(hs.config)
        parts = [part for part in (key or '').split('.') if part]
        for depth, part in enumerate(parts):
            if not isinstance(value, dict) or part not in value:
                raise NotFound(f"no configuration value {'.'.join(parts[:depth + 1])!r}")
            value = value[part]
        source = None
        if parts and not isinstance(value, dict):
            try:
                source = hs.config.which(*parts)
            except Exception:
                source = None
        name = parts[-1] if parts else ''
        return cls.redact(cls.plain(value), name, exported='export' in [part.lower() for part in parts]), source

    @classmethod
    def plain(cls, value: Any) -> Any:
        """Namespaces to plain dictionaries, recursively."""
        if isinstance(value, dict):
            return {str(key): cls.plain(item) for key, item in value.items()}
        return value


def hostname() -> str:
    return socket.gethostname()


def site_environment() -> dict[str, str]:
    """The environment a child `hs` process needs to act on the same site as this server."""
    return {name: value for name, value in os.environ.items()}
