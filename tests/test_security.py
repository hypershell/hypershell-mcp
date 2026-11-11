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


# -- One caller's regular expression must not stall everyone ------------------------------------

async def test_a_catastrophic_pattern_is_abandoned_not_obeyed(client):
    """`(a|aa)+$` against a run of 'a' would take longer than the universe has. Here: a moment."""
    await client.call_tool('submit_tasks', {'commands': ['echo ' + 'a' * 60 + '!', 'echo fine']})
    started = time.monotonic()
    with pytest.raises(ToolError, match='took too long'):
        await client.call_tool('list_tasks', {'select': {'where': ['args ~ (a|aa)+$']}})
    assert time.monotonic() - started < 5
    # The database thread is free again straight away, and ordinary patterns still work.
    page = data(await client.call_tool('list_tasks', {'select': {'where': ['args ~ ^echo f']}, 'fields': ['args']}))
    assert [task['args'] for task in page['tasks']] == ['echo fine']


async def test_patterns_are_checked_before_use(client):
    with pytest.raises(ToolError, match='not a valid regular expression'):
        await client.call_tool('list_tasks', {'select': {'where': ['args ~ (']}})
    with pytest.raises(ToolError, match='limit is 300'):
        await client.call_tool('list_tasks', {'select': {'where': ['args ~ ' + 'a' * 400]}})
    with pytest.raises(ToolError, match='is not text'):
        await client.call_tool('list_tasks', {'select': {'where': ['duration ~ 5']}})


def test_output_search_is_bounded_in_time_and_line_length(tmp_path):
    from hypershell_mcp.outputs import read_output
    roots = (tmp_path,)

    def search(path, pattern):
        return read_output('t', 'stdout', str(path), roots=roots, status='failed', client_host=None,
                           local_host='h', pattern=pattern)

    evil = tmp_path / 'evil.out'
    evil.write_text('a' * 60 + '!\n')
    started = time.monotonic()
    chunk = search(evil, '(a|aa)+$')
    assert chunk.available is False and 'abandoned' in chunk.reason and time.monotonic() - started < 8
    assert 'limit is 300' in search(evil, 'a' * 400).reason
    one_line = tmp_path / 'one-line.out'
    one_line.write_text('x' * 200_000 + 'NEEDLE' + 'x' * 10)   # no newline anywhere
    found = search(one_line, 'NEEDLE')
    assert found.available and found.matches == 1 and len(found.text) < 70_000


# -- Selectors ---------------------------------------------------------------------------------

async def test_boolean_fields_can_be_compared(client):
    await client.call_tool('submit_tasks', {'commands': ['x']})
    page = data(await client.call_tool('list_tasks', {'select': {'where': ['retried == false']}}))
    assert page['total'] == 1
    assert data(await client.call_tool('list_tasks', {'select': {'where': ['retried != false']}}))['total'] == 0
    with pytest.raises(ToolError, match='only be compared with == or !='):
        await client.call_tool('list_tasks', {'select': {'where': ['retried > false']}})


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
    assert [cluster.id for cluster in manager.list()] == [good.name]
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


@pytest.mark.integration
async def test_run_tasks_submits_nothing_when_it_cannot_start(client, tmp_path):
    with pytest.raises(ToolError, match='nothing was submitted'):
        await client.call_tool('run_tasks', {'commands': ['echo should-not-be-queued'], 'cwd': str(tmp_path / 'nope')})
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 0
