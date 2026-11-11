# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
What a caller must not be able to do, each as a test that tries.

These came out of an adversarial review of the first build. Each one was a working exploit or a
way to break the site for everyone; they are kept as tests so that none of them comes back.
"""

import asyncio
import json
import time

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client

from conftest import data
from hypershell_mcp.backend import Backend
from hypershell_mcp.templates import TemplateError, compile_template

pytestmark = pytest.mark.unit


# -- Submit must never execute anything ---------------------------------------------------------

CODE_TEMPLATES = ['{% touch MARK %}', '{ % touch MARK % } {}', '{  %touch MARK%  }', '{\t% touch MARK %}',
                  '{= __import__("os").getpid() =}', '{ = open(x).read() = }', '{ =1+1= } {}', '{=x=}']


@pytest.mark.parametrize('template', CODE_TEMPLATES)
async def test_no_template_runs_code_at_submit_however_it_is_padded(secured_server, token, tmp_path, template):
    """A submit-only caller once ran shell and Python in the server by padding the braces."""
    marker = tmp_path / 'executed'
    template = template.replace('MARK', str(marker))
    async with asgi_client(secured_server, auth=token('submit')) as client:
        for dry_run in (True, False):
            with pytest.raises(ToolError, match='would run on the server'):
                await client.call_tool('submit_tasks', {'commands': [str(tmp_path)], 'template': template,
                                                         'dry_run': dry_run})
    assert not marker.exists()
    async with asgi_client(secured_server, auth=token('read')) as client:
        assert data(await client.call_tool('status', {}))['tasks']['total'] == 0


def test_templates_only_rearrange_text():
    expand = compile_template('align --in {} --out { /- }.bam --dir {.} --up {..} --ext {+}/{++} --stem {-}')
    assert expand('data/run1/a.fastq') == ('align --in data/run1/a.fastq --out a.bam --dir data/run1 --up data '
                                           '--ext .fastq/fastq --stem data/run1/a')
    words = compile_template('first={[0]} rest={[1:]} last={[-1]} every-other={[::2]} pair={[1:3]}')
    assert words('a b c d') == 'first=a rest=b c d last=d every-other=a c pair=b c'
    # Braces that are not patterns are ordinary text: shell, awk and JSON all use them.
    literal = compile_template("awk '{print $1}' {} | jq '{name: .n}' ${HOME} {name}")
    assert literal('f.txt') == "awk '{print $1}' f.txt | jq '{name: .n}' ${HOME} {name}"
    with pytest.raises(TemplateError, match='asks for word 5'):
        compile_template('{[5]}')('a b')
    with pytest.raises(TemplateError, match='no pattern'):
        compile_template('echo hello')
    with pytest.raises(TemplateError, match='step of zero'):
        compile_template('{[::0]}')


def test_a_hostile_template_cannot_take_long():
    """Unclosed braces and padded slices once cost time quadratic in the template's length."""
    with pytest.raises(TemplateError, match='limit is 2000'):
        compile_template('{' * 100_000)
    started = time.monotonic()
    for hostile in ('{' * 2000, '{[' + ' ' * 1990 + 'x]}', '{' + ' ' * 1990 + '}', '{[' + '1:' * 900 + ']} {}',
                    '{[' + '9' * 1900 + ']}'):
        try:
            compile_template(hostile)('a b c')
        except TemplateError:
            pass
    assert time.monotonic() - started < 1
    assert compile_template('{[ 1 : 3 ]}')('a b c d') == 'b c'


async def test_submissions_are_bounded_in_size(client):
    with pytest.raises(ToolError, match='limit is 65536'):
        await client.call_tool('submit_tasks', {'commands': ['x' * 70_000]})
    with pytest.raises(ToolError, match='limit is 65536'):
        await client.call_tool('submit_tasks', {'commands': ['y' * 40_000], 'template': '{} {}'})
    with pytest.raises(ToolError, match='more than a task may carry'):
        await client.call_tool('submit_tasks', {'commands': ['x'], 'tags': {f'k{n}': n for n in range(65)}})
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 0


async def test_dry_run_cannot_be_used_to_read_files(client, tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('TOP-SECRET')
    for template in ('{ =open(x).read()= }', '{ % cat @ % }'):
        with pytest.raises(ToolError) as refused:
            await client.call_tool('submit_tasks', {'commands': [str(secret)], 'template': template, 'dry_run': True})
        assert 'TOP-SECRET' not in str(refused.value)


# -- Tags are labels, not a side door into task options or attribution --------------------------

@pytest.mark.parametrize('name', ['group', 'cores', 'memory', 'timeout'])
async def test_option_names_are_refused_as_tags(client, name):
    """The names of typed task fields cannot be used as tags."""
    for arguments in ({'commands': ['x'], 'tags': {name: 'control'}},
                      {'commands': [{'command': 'x', 'tags': {name: 'control'}}]}):
        with pytest.raises(ToolError, match='reads it as a task option'):
            await client.call_tool('submit_tasks', arguments)
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 0


async def test_submitter_and_batch_cannot_be_forged(secured_server, token):
    async with asgi_client(secured_server, auth=token('all', subject='mallory')) as client:
        for arguments in ({'commands': ['x'], 'tags': {'submitter': 'ada'}},
                          {'commands': [{'command': 'x', 'tags': {'submitter': 'ada'}}]},
                          {'commands': [{'command': 'x', 'tags': {'batch': 'someone-elses'}}]}):
            with pytest.raises(ToolError, match='set by the server'):
                await client.call_tool('submit_tasks', arguments)
        created = data(await client.call_tool('submit_tasks', {'commands': ['x'], 'batch': 'mine'}))
        (task,) = data(await client.call_tool('list_tasks', {'fields': ['tag']}))['tasks']
        assert task['tag'] == {'batch': 'mine', 'submitter': 'mallory'} and created['batch'] == 'mine'
        for change in ({'set': {'submitter': 'ada'}}, {'remove': ['submitter']}):
            with pytest.raises(ToolError, match='set by the server'):
                await client.call_tool('tag_tasks', {'select': {'batch': 'mine'}, **change})
        # A batch is a grouping, and regrouping existing tasks is what the manage tier is for.
        moved = data(await client.call_tool('tag_tasks', {'select': {'batch': 'mine'}, 'set': {'batch': 'other'}}))
        assert moved['changed'] == 1


# -- One caller's pattern or query must not stall everyone --------------------------------------

HOSTILE_PATTERNS = ['(a|aa)+$', '(a+)+$', '(((((((a{9}){9}){9}){9}){9}){9}){9})', '((a{999}){999}){999}',
                    r'(?=a)a', r'(a)\1', 'a' * 400, '(']


async def test_no_pattern_can_stall_or_exhaust_the_server(client):
    """
    A backtracking engine can be made to run for hours by `(a|aa)+$`, and to allocate gigabytes
    just compiling nested `{9}` repeats. Callers' patterns go to RE2, which does neither: each of
    these is answered or refused in milliseconds.
    """
    await client.call_tool('submit_tasks', {'commands': ['echo ' + 'a' * 60 + '!', 'echo fine']})
    started = time.monotonic()
    for pattern in HOSTILE_PATTERNS:
        try:
            page = data(await client.call_tool('list_tasks', {'select': {'where': [f'args ~ {pattern}']}}))
            assert page['total'] in (0, 1), pattern
        except ToolError as refused:
            assert 'cannot be used as a pattern' in str(refused) or 'limit is 300' in str(refused), pattern
    assert time.monotonic() - started < 3
    page = data(await client.call_tool('list_tasks', {'select': {'where': ['args ~ ^echo f']}, 'fields': ['args']}))
    assert [task['args'] for task in page['tasks']] == ['echo fine']


async def test_patterns_are_refused_where_they_cannot_be_bounded(client, monkeypatch):
    """Without the linear matcher and the deadline on the connection, no caller pattern reaches the database."""
    from hypershell_mcp.backend import Prepared
    await client.call_tool('submit_tasks', {'commands': ['echo a.bam']})
    assert data(await client.call_tool('list_tasks', {'select': {'where': ['args ~ bam$']}}))['total'] == 1
    for missing in ('patterns', 'deadline'):
        with monkeypatch.context() as patch:
            patch.setattr(Prepared, missing, False)
            for condition in ('args ~ bam$', 'args !~ bam$'):
                with pytest.raises(ToolError, match='not offered on this database'):
                    await client.call_tool('list_tasks', {'select': {'where': [condition]}})
            assert data(await client.call_tool('list_tasks', {'select': {'where': ['exit_status == null']}}))['total'] == 1


async def test_patterns_are_checked_before_use(client):
    with pytest.raises(ToolError, match='cannot be used as a pattern: missing'):
        await client.call_tool('list_tasks', {'select': {'where': ['args ~ (']}})
    with pytest.raises(ToolError, match='limit is 300'):
        await client.call_tool('list_tasks', {'select': {'where': ['args ~ ' + 'a' * 400]}})
    with pytest.raises(ToolError, match='is not text'):
        await client.call_tool('list_tasks', {'select': {'where': ['duration ~ 5']}})


def test_output_search_is_bounded(tmp_path):
    from hypershell_mcp.outputs import read_output
    roots = (tmp_path,)

    def search(path, pattern):
        return read_output('t', 'stdout', str(path), roots=roots, status='failed', client_host=None,
                           local_host='h', pattern=pattern)

    evil = tmp_path / 'evil.out'
    evil.write_text('a' * 5000 + '!\n')
    started = time.monotonic()
    for pattern in HOSTILE_PATTERNS:
        chunk = search(evil, pattern)
        assert chunk.available is False or chunk.matches in (0, 1), pattern
    assert time.monotonic() - started < 3
    assert 'limit is 300' in search(evil, 'a' * 400).reason
    assert 'cannot be used as a pattern' in search(evil, '(((((((a{9}){9}){9}){9}){9}){9}){9})').reason
    one_line = tmp_path / 'one-line.out'
    one_line.write_text('x' * 200_000 + 'NEEDLE' + 'x' * 10)   # no newline anywhere
    found = search(one_line, 'NEEDLE')
    assert found.available and found.matches == 1 and len(found.text) < 70_000


async def test_a_read_that_runs_too_long_is_stopped(monkeypatch):
    """One slow statement would hold the only database thread, and so every other caller."""
    from hypershell_mcp import backend
    from hypershell_mcp.backend import Backend, BackendError, lib
    monkeypatch.setattr(backend, 'QUERY_SECONDS', 0.3)

    def endless() -> int:
        return lib().Session.execute(lib().sa.text(
            'with recursive c(x) as (select 1 union all select x + 1 from c where x < 2000000000) '
            'select count(*) from c')).scalar()

    engine = Backend()
    started = time.monotonic()
    with pytest.raises(BackendError, match='was stopped'):
        await engine.call(endless)
    assert time.monotonic() - started < 5
    assert (await engine.call(engine.count, None)).total == 0   # and the thread is usable again


# -- Selectors ---------------------------------------------------------------------------------

async def test_boolean_fields_can_be_compared(client):
    await client.call_tool('submit_tasks', {'commands': ['x']})
    page = data(await client.call_tool('list_tasks', {'select': {'where': ['retried == false']}}))
    assert page['total'] == 1
    assert data(await client.call_tool('list_tasks', {'select': {'where': ['retried != false']}}))['total'] == 0
    with pytest.raises(ToolError, match='only be compared with == or !='):
        await client.call_tool('list_tasks', {'select': {'where': ['retried > false']}})


async def test_tag_values_are_all_checked(client):
    """A value that follows an empty one is checked like any other."""
    for tags in ({'a': '', 'zz': 'has space; $(id)'}, {'a': '', 'b': 'v' * 200}, {'k': 'two words'},
                 {'k': ['a', 'list']}):
        with pytest.raises(ToolError):
            await client.call_tool('submit_tasks', {'commands': ['x'], 'tags': tags})
    assert data(await client.call_tool('submit_tasks', {
        'commands': ['x'], 'tags': {'empty': '', 'n': 3, 'ok': True, 'f': 0.5, 'word': 'a.b-c_d+e'}}))['submitted'] == 1


@pytest.mark.parametrize('text', ['64G', '64gib', '1.5GiB', '1.5', '4 gigs', '-1GB', 'GB'])
async def test_memory_sizes_in_other_forms_are_refused(client, text):
    """A size is a whole number and one of the units B, KB, MB, GB, TB; nothing else."""
    from hypershell_mcp.clusters import ClusterError, ClusterOptions
    with pytest.raises(ToolError, match='as memory'):
        await client.call_tool('submit_tasks', {'commands': ['x'], 'memory': text})
    with pytest.raises(ClusterError, match='as memory'):
        ClusterOptions(client_memory=text).arguments()


# -- Configuration never shows a secret ---------------------------------------------------------

def test_redaction_covers_exports_urls_and_lists():
    config = {
        'export': {'DB_CONN': 'postgresql://svc:s3cr3t@db/prod', 'WANDB_API': 'wandb-abcdef', 'THREADS': 4,
                   'nested': {'x': 'also-hidden'}},
        'database': {'url': 'postgresql://user:hun/ter2@db.example/hs', 'file': '/data/task.db',
                     'replica': 'libsql://db.example/x?authToken=eyJhbGciOi', 'port': 5432,
                     'dsn': 'postgresql://db.example/hs?user=u&password=hunter2'},
        'ssh': {'nodelist': [{'host': 'a', 'password': 'hunter2'}, 'ssh://root:toor@b']},
        'server': {'auth': 'correct-horse', 'tls': {'key': '<auto>', 'enabled': True}, 'port': 50001},
    }
    masked = Backend.redact(config)
    text = json.dumps(masked)
    for secret in ('s3cr3t', 'wandb-abcdef', 'also-hidden', 'hun/ter2', 'eyJhbGciOi', 'hunter2', 'toor', 'correct-horse'):
        assert secret not in text, secret
    assert masked['export'] == {'DB_CONN': '********', 'WANDB_API': '********', 'THREADS': '********',
                                'nested': {'x': '********'}}
    assert masked['database'] == {'url': 'postgresql://********@db.example/hs', 'file': '/data/task.db',
                                  'replica': 'libsql://db.example/x?********', 'port': 5432,
                                  'dsn': 'postgresql://db.example/hs?********'}
    assert masked['ssh']['nodelist'] == [{'host': 'a', 'password': '********'}, 'ssh://********@b']
    assert masked['server'] == {'auth': '********', 'tls': {'key': '<auto>', 'enabled': True}, 'port': 50001}
    assert Backend.redact({'A': 'x'}, 'export') == {'A': '********'}


def test_redaction_covers_secret_tables_bare_credentials_and_fragments():
    masked = Backend.redact({
        'misc': {'credentials': {'value': 'hunter2', 'user': 'ada'}, 'passphrase': 'open sesame', 'pwd': 'x1'},
        'ssh': {'args': '-o ProxyCommand="nc -X connect -x proxy:8080 svc:s3cr3t@gate %h %p"', 'bare': 'user:pw@host'},
        'hook': 'https://h.example/cb#access_token=abc123',
        'signature': 'deadbeef', 'session': {'cookie': 'c00kie'}, 'plain': 'user@host', 'ratio': '3:4'})
    text = json.dumps(masked)
    for secret in ('hunter2', 'open sesame', 'x1', 's3cr3t', 'user:pw', 'abc123', 'deadbeef', 'c00kie'):
        assert secret not in text, secret
    assert masked['misc']['credentials'] == {'value': '********', 'user': '********'}
    assert masked['hook'] == 'https://h.example/cb#********'
    assert masked['plain'] == 'user@host' and masked['ratio'] == '3:4'


# -- Clusters ----------------------------------------------------------------------------------

def test_cluster_options_cannot_smuggle_arguments():
    from hypershell_mcp.clusters import ClusterError, ClusterOptions
    for options, message in (
            ({'launcher': 'ssh', 'hosts': ['-oProxyCommand=evil']}, 'invalid host'),
            ({'launcher': 'ssh', 'hosts': ['good', 'bad host']}, 'invalid host'),
            ({'launcher': 'ssh', 'ssh_group': '--no-tls'}, 'invalid ssh group'),
            ({'zone': '--no-tls'}, 'invalid zone'),
            ({'client_memory': '--initdb'}, 'cannot read'),
    ):
        with pytest.raises(ClusterError, match=message):
            ClusterOptions(**options).arguments()
    for options, message in (({'hosts': ['a']}, "need launcher 'ssh'"), ({'ssh_group': 'g'}, "need launcher 'ssh'"),
                             ({'launcher_command': 'srun'}, "needs launcher 'custom'")):
        with pytest.raises(ClusterError, match=message):
            ClusterOptions(**options).arguments()
    # An autoscaling cluster never drains, whatever mode says; record what will actually happen.
    scaling = ClusterOptions(launcher='custom', launcher_command='srun', autoscaling='dynamic')
    assert scaling.effective_mode == 'forever' and '--forever' in scaling.arguments()
    # A template may be anything, since running commands is what the run tier is for; it travels
    # as one word and so cannot be mistaken for another option.
    assert '--template=--no-db {}' in ClusterOptions(template='--no-db {}').arguments()


def test_a_damaged_cluster_record_does_not_hide_the_others(tmp_path):
    from hypershell_mcp.clusters import ClusterError, ClusterManager
    manager = ClusterManager(tmp_path)
    good, bad, empty = tmp_path / '20260101-000000-aaaa', tmp_path / '20260102-000000-bbbb', tmp_path / '20260103-000000-cccc'
    for folder in (good, bad, empty):
        folder.mkdir()
    good.joinpath('cluster.json').write_text(json.dumps({
        'id': good.name, 'pid': 2 ** 22 + 12345, 'started': '2026-01-01 00:00:00', 'mode': 'drain', 'workers': 1,
        'launcher': None, 'zone': None, 'cwd': '/', 'command': 'hs cluster'}))
    bad.joinpath('cluster.json').write_text('{"id": "trunc')
    empty.joinpath('cluster.json').write_text('')
    # A record from another version may carry fields this one does not know, or lack some it does.
    newer = tmp_path / '20260104-000000-dddd'
    newer.mkdir()
    newer.joinpath('cluster.json').write_text(json.dumps({
        'id': newer.name, 'pid': 2 ** 22 + 12346, 'started': '2026-01-04 00:00:00', 'mode': 'drain', 'workers': 1,
        'command': 'hs cluster', 'added_later': {'x': 1}}))
    wrong = tmp_path / '20260105-000000-eeee'
    wrong.mkdir()
    wrong.joinpath('cluster.json').write_text(json.dumps({'id': wrong.name, 'pid': 1, 'workers': 'many'}))
    assert [cluster.id for cluster in manager.list()] == [newer.name, good.name]
    assert manager.running() == [] and manager.last_exit() is not None
    with pytest.raises(ClusterError, match='unreadable'):
        manager.get(bad.name)
    for name in ('../escape', 'a/b', 'missing'):
        with pytest.raises(ClusterError, match='no cluster'):
            manager.get(name)


@pytest.mark.integration
async def test_two_starts_at_once_make_one_cluster(client):
    options = {'options': {'mode': 'forever', 'workers': 1}}
    outcomes = await asyncio.gather(client.call_tool('start_cluster', options),
                                    client.call_tool('start_cluster', options), return_exceptions=True)
    started = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(started) == 1 and len(refused) == 1 and 'already running' in str(refused[0])
    running = data(await client.call_tool('status', {}))['clusters']
    assert [cluster['id'] for cluster in running] == [data(started[0])['id']]
    await client.call_tool('stop_cluster', {'how': 'kill'})


def test_secrets_named_by_the_configuration_do_not_reach_children(monkeypatch):
    from hypershell_mcp import clusters
    monkeypatch.setenv('HYPERSHELL_MCP_JWT_SECRET', 'a')
    monkeypatch.setenv('MY_SIGNING_SECRET', 'b')
    monkeypatch.setenv('UNRELATED', 'c')
    monkeypatch.setattr(clusters, 'PROTECTED', set())
    clusters.protect('MY_SIGNING_SECRET')
    environment = clusters.child_environment()
    assert 'HYPERSHELL_MCP_JWT_SECRET' not in environment and 'MY_SIGNING_SECRET' not in environment
    assert environment['UNRELATED'] == 'c' and 'HYPERSHELL_SITE' in environment


@pytest.mark.integration
async def test_run_tasks_reports_a_cluster_that_cannot_start_and_keeps_the_batch(client, tmp_path):
    """The tasks are queued by then; raising would hide the batch and invite a duplicate submit."""
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0o000)
    try:
        result = data(await client.call_tool('run_tasks', {'commands': ['echo queued'], 'cwd': str(locked)}))
    finally:
        locked.chmod(0o700)
    assert result['done'] is False and result['batch'] and 'no cluster could be started' in result['note']
    assert data(await client.call_tool('status', {}))['tasks']['pending'] == 1
    assert data(await client.call_tool('list_clusters', {}))['clusters'] == [] or all(
        cluster['state'] == 'exited' for cluster in data(await client.call_tool('list_clusters', {}))['clusters'])


@pytest.mark.integration
async def test_run_tasks_submits_nothing_when_it_cannot_start(client, tmp_path):
    with pytest.raises(ToolError, match='nothing was submitted'):
        await client.call_tool('run_tasks', {'commands': ['echo should-not-be-queued'], 'cwd': str(tmp_path / 'nope')})
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 0
