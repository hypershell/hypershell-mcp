# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Submitting, selecting, summarising and changing tasks, through an MCP client."""

import json

import pytest
from fastmcp.exceptions import ToolError

from conftest import data

pytestmark = pytest.mark.unit


async def submit(client, commands, **options) -> dict:
    return data(await client.call_tool('submit_tasks', {'commands': commands, **options}))


async def listed(client, **arguments) -> list[dict]:
    return data(await client.call_tool('list_tasks', arguments))['tasks']


def finish(args: str, exit_status: int, *, host: str = 'node1', duration: int = 5) -> None:
    """Mark a submitted task as having run, as a client's result would."""
    from datetime import datetime

    from hypershell.data.core import Session
    from hypershell.data.model import Task
    now = datetime.now().astimezone()
    task = Session.query(Task).filter(Task.args == args).one()
    Task.update(task.id, schedule_time=now, start_time=now, completion_time=now, exit_status=exit_status,
                client_host=host, duration=duration, waited=1)
    Session.remove()


async def test_status_on_an_empty_site_says_what_to_do(client):
    status = data(await client.call_tool('status', {}))
    assert status['tasks']['total'] == 0
    assert status['access'] == {'subject': None, 'transport': 'stdio', 'token_expires': None,
                                'tiers': ['read', 'submit', 'manage', 'run', 'admin']}
    assert 'no tasks' in status['hints'][0]
    assert status['hypershell_version'].startswith('2.9')


async def test_commands_are_stored_verbatim_including_hash(client):
    """HyperShell's own submit strips everything after a '#'. This server must not."""
    command = "echo 'issue #42' # HYPERSHELL: cores:64"
    await submit(client, [command])
    (task,) = await listed(client, fields=['args', 'cores', 'tag'])
    assert task['args'] == command
    assert task['cores'] is None


async def test_submit_returns_a_batch_that_selects_exactly_those_tasks(client):
    first = await submit(client, ['a', 'b'], tags={'project': 'atlas'})
    second = await submit(client, ['c'])
    assert first['submitted'] == 2 and len(first['ids']) == 2 and first['batch'] != second['batch']
    assert first['group'] == 0 and first['zone'] == 'default'
    tasks = await listed(client, select={'batch': first['batch']}, order_by='args', descending=False)
    assert [task['args'] for task in tasks] == ['a', 'b']
    assert all(task['tag'] == {'project': 'atlas', 'batch': first['batch']} for task in tasks)
    assert all(task['status'] == 'pending' for task in tasks)
    more = await submit(client, ['d'], batch=first['batch'])
    assert more['batch'] == first['batch']
    assert len(await listed(client, select={'batch': first['batch']})) == 3


async def test_per_task_options_and_shared_options(client):
    await submit(client, ['plain', {'command': 'special', 'tags': {'alpha': 0.5}, 'cores': 8, 'memory': '2GB',
                                    'group': 3, 'timeout': 60}],
                 tags={'sweep': 's1'}, cores=2, timeout=600, zone='gpu')
    tasks = {task['args']: task for task in await listed(
        client, fields=['args', 'cores', 'memory', 'timeout', 'group', 'zone', 'tag'])}
    assert (tasks['plain']['cores'], tasks['plain']['timeout'], tasks['plain']['group']) == (2, 600, 0)
    assert (tasks['special']['cores'], tasks['special']['memory'], tasks['special']['timeout']) == (8, 2 * 1024 ** 3, 60)
    assert tasks['special']['group'] == 3 and tasks['special']['tag']['alpha'] == 0.5
    assert tasks['plain']['zone'] == tasks['special']['zone'] == 'gpu'


async def test_template_builds_commands_and_refuses_code(client):
    await submit(client, ['data/run1/a.fastq', 'b.fastq'], template='align --in {} --out {/-}.bam')
    assert sorted(task['args'] for task in await listed(client, fields=['args'])) == [
        'align --in b.fastq --out b.bam', 'align --in data/run1/a.fastq --out a.bam']
    for unsafe in ('{% rm -rf @ %}', 'echo {= __import__("os").getpid() =}'):
        with pytest.raises(ToolError, match='would run on the server'):
            await submit(client, ['x'], template=unsafe)


async def test_bad_submissions_are_explained(client):
    with pytest.raises(ToolError, match='one line'):
        await submit(client, ['echo a\necho b'])
    with pytest.raises(ToolError, match="cannot read 'lots' as memory"):
        await submit(client, ['x'], memory='lots')
    with pytest.raises(ToolError, match='cannot submit'):
        await submit(client, ['x'], tags={'bad key!': 1})
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 0


async def test_selectors_combine_status_tags_group_zone_and_conditions(client):
    await submit(client, [{'command': f'job {n}', 'tags': {'n': n, 'even': n % 2 == 0}} for n in range(6)])
    await submit(client, ['other'], group=2, zone='gpu', tags={'kind': 'x'})
    for n, status in ((0, 0), (1, 0), (2, 3), (3, 137)):
        finish(f'job {n}', status, host='node1' if n < 2 else 'node2', duration=10 * (n + 1))

    async def args(**select):
        return sorted(task['args'] for task in await listed(client, select=select, fields=['args']))

    assert await args(status=['succeeded']) == ['job 0', 'job 1']
    assert await args(status=['failed']) == ['job 2', 'job 3']
    assert await args(status=['pending']) == ['job 4', 'job 5', 'other']
    assert await args(status=['completed']) == ['job 0', 'job 1', 'job 2', 'job 3']
    assert await args(status=['failed', 'pending'], tags={'even': True}) == ['job 2', 'job 4']
    assert await args(tags={'kind': None}) == ['other']
    assert await args(tags={'n': 3}) == ['job 3']
    assert await args(group=2) == await args(zone='gpu') == ['other']
    assert len(await args(zone='default')) == 6
    assert await args(where=['exit_status == 137']) == ['job 3']
    assert await args(where=['duration >= 30']) == ['job 2', 'job 3']
    assert await args(where=['duration <= 20', 'client_host == node1']) == ['job 0', 'job 1']
    assert await args(where=['args ~ ^job [45]$']) == ['job 4', 'job 5']
    assert await args(where=['args !~ job']) == ['other']
    assert await args(where=['exit_status != null', 'exit_status != 0']) == ['job 2', 'job 3']
    assert await args(where=['zone != default']) == ['other']


async def test_bad_selectors_are_explained(client):
    for select, message in (({'where': ['nonsense']}, 'cannot read condition'),
                            ({'where': ['colour == red']}, 'unknown field'),
                            ({'where': ['duration > long']}, 'cannot read'),
                            ({'where': ['tag == x']}, 'selector.tags'),
                            ({'ids': ['12']}, 'not a task id')):
        with pytest.raises(ToolError, match=message):
            await client.call_tool('list_tasks', {'select': select})
    with pytest.raises(ToolError, match='unknown field'):
        await client.call_tool('list_tasks', {'fields': ['id', 'colour']})


async def test_listing_pages_and_shortens_long_commands(client):
    await submit(client, [f'task {n:02d}' for n in range(7)] + ['x' * 1000])
    page = data(await client.call_tool('list_tasks', {'limit': 3, 'order_by': 'args', 'descending': False,
                                                       'fields': ['args']}))
    assert (page['total'], page['count'], page['next_offset']) == (8, 3, 3)
    last = data(await client.call_tool('list_tasks', {'limit': 3, 'offset': 6, 'order_by': 'args',
                                                       'descending': False, 'fields': ['args']}))
    assert last['next_offset'] is None and last['count'] == 2
    assert last['tasks'][1]['args'].endswith('(+600 chars)')


async def test_summaries_group_by_fields_and_tags(client):
    await submit(client, [{'command': f'fit {n}', 'tags': {'model': 'a' if n < 3 else 'b'}} for n in range(5)])
    for n, status, duration in ((0, 0, 10), (1, 0, 20), (2, 1, 30), (3, 1, 40)):
        finish(f'fit {n}', status, duration=duration)
    summary = data(await client.call_tool('summarize_tasks', {'group_by': ['tag:model', 'status']}))
    rows = {(row['key']['tag:model'], row['key']['status']): row for row in summary['rows']}
    assert summary['total'] == 5
    assert rows[('a', 'succeeded')]['count'] == 2 and rows[('a', 'succeeded')]['duration_mean'] == 15
    assert rows[('b', 'failed')]['duration_max'] == 40 and rows[('b', 'pending')]['count'] == 1
    totals = data(await client.call_tool('summarize_tasks', {'group_by': [], 'select': {'status': ['completed']}}))
    assert totals['rows'][0]['count'] == 4 and totals['rows'][0]['duration_total'] == 100
    with pytest.raises(ToolError, match='cannot group by'):
        await client.call_tool('summarize_tasks', {'group_by': ['args']})


async def test_tags_can_be_listed(client):
    await submit(client, ['a', 'b'], tags={'project': 'atlas'}, batch='one')
    await submit(client, ['c'], tags={'project': 'boson'}, batch='two')
    assert data(await client.call_tool('list_tags', {}))['keys'] == ['batch', 'project']
    assert data(await client.call_tool('list_tags', {'key': 'project'}))['values'] == {'atlas': 2, 'boson': 1}
    assert data(await client.call_tool('list_tags', {'select': {'batch': 'two'}}))['keys'] == ['batch', 'project']


async def test_get_task_explains_status_and_missing_output(client):
    created = await submit(client, ['boom'])
    finish('boom', 137)
    detail = data(await client.call_tool('get_task', {'task_id': created['ids'][0]}))
    assert detail['task']['status'] == 'failed' and 'out of memory' in detail['task']['status_detail']
    assert detail['stdout']['available'] is False and 'not capturing' in detail['stdout']['reason']
    with pytest.raises(ToolError, match='No task with id'):
        await client.call_tool('get_task', {'task_id': '0193f1c2-7d2a-7c7e-9d3b-2f6a1b0c4d5e'})


async def test_cancel_retry_and_tag(client):
    batch = (await submit(client, ['one', 'two', 'three']))['batch']
    finish('one', 0)
    select = {'batch': batch}

    preview = data(await client.call_tool('cancel_tasks', {'select': select, 'dry_run': True}))
    assert (preview['matched'], preview['changed'], preview['skipped'], preview['dry_run']) == (3, 0, 1, True)
    assert data(await client.call_tool('status', {}))['tasks']['cancelled'] == 0

    cancelled = data(await client.call_tool('cancel_tasks', {'select': select}))
    assert (cancelled['changed'], cancelled['skipped']) == (2, 1)
    assert data(await client.call_tool('status', {}))['tasks'] == {
        'pending': 0, 'running': 0, 'succeeded': 1, 'failed': 0, 'cancelled': 2, 'total': 3}

    # With no status given, retry means the failed ones: cancelled and succeeded tasks stay put.
    assert data(await client.call_tool('retry_tasks', {'select': select}))['changed'] == 0
    retried = data(await client.call_tool('retry_tasks', {'select': {**select, 'status': ['cancelled']}}))
    assert retried['changed'] == 2
    assert data(await client.call_tool('status', {}))['tasks']['pending'] == 2

    tagged = data(await client.call_tool('tag_tasks', {'select': select, 'set': {'reviewed': True, 'note': 'ok'},
                                                        'remove': ['batch']}))
    assert tagged['changed'] == 3
    assert all(task['tag'] == {'reviewed': True, 'note': 'ok'} for task in await listed(client, fields=['tag']))
    with pytest.raises(ToolError, match='cannot be set as tags'):
        await client.call_tool('tag_tasks', {'select': {'tags': {'note': 'ok'}}, 'set': {'cores': 4}})


async def test_delete_previews_first_and_never_takes_everything_by_accident(client):
    batch = (await submit(client, ['keep', 'drop 1', 'drop 2']))['batch']
    select = {'batch': batch, 'where': ['args ~ ^drop']}
    preview = data(await client.call_tool('delete_tasks', {'select': select}))
    assert preview['dry_run'] is True and preview['changed'] == 0 and 'Preview only: 2 tasks' in preview['note']
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 3
    deleted = data(await client.call_tool('delete_tasks', {'select': select, 'confirm': True}))
    assert deleted['changed'] == 2
    assert [task['args'] for task in await listed(client, fields=['args'])] == ['keep']
    for tool in ('delete_tasks', 'cancel_tasks', 'tag_tasks'):
        with pytest.raises(ToolError, match='every task'):
            await client.call_tool(tool, {'select': {}, 'confirm': True} if tool == 'delete_tasks'
                                   else {'select': {}, 'set': {'a': 1}} if tool == 'tag_tasks' else {'select': {}})
    assert data(await client.call_tool('delete_tasks', {'select': {}, 'confirm': True, 'everything': True}))['changed'] == 1


async def test_wait_returns_at_once_when_done_and_does_not_hang_on_an_unserved_queue(client):
    batch = (await submit(client, ['done']))['batch']
    finish('done', 0)
    waited = data(await client.call_tool('wait_for_tasks', {'select': {'batch': batch}, 'timeout': 30}))
    assert waited['done'] is True and waited['counts']['succeeded'] == 1 and waited['waited_seconds'] < 2
    pending = (await submit(client, ['never']))['batch']
    waited = data(await client.call_tool('wait_for_tasks', {'select': {'batch': pending}, 'timeout': 30}))
    assert waited['done'] is False and 'nothing is running them' in waited['note'] and waited['waited_seconds'] < 10
    assert 'No tasks match' in data(await client.call_tool('wait_for_tasks', {'select': {'batch': 'nope'}}))['note']


async def test_configuration_is_readable_with_secrets_masked(client):
    from hypershell_mcp.backend import Backend
    masked = Backend.redact({
        'server': {'auth': 'correct-horse-battery', 'port': 50001, 'tls': {'key': '<auto>', 'enabled': True}},
        'database': {'password': 'hunter2', 'url': 'postgresql://ada:hunter2@db/hs', 'file': '/data/task.db'},
        'mcp': {'jwt': {'secret': 'x' * 40, 'secretfile': '/etc/key', 'issuer': 'lab'}},
        'logging': {'file': {'keep': 3}}})
    assert 'correct-horse-battery' not in json.dumps(masked) and 'hunter2' not in json.dumps(masked)
    assert masked['server'] == {'auth': '********', 'port': 50001, 'tls': {'key': '<auto>', 'enabled': True}}
    assert masked['database'] == {'password': '********', 'url': 'postgresql://********@db/hs', 'file': '/data/task.db'}
    assert masked['mcp']['jwt']['secret'] == '********' and masked['mcp']['jwt']['issuer'] == 'lab'
    assert masked['logging']['file']['keep'] == 3
    assert data(await client.call_tool('get_config', {'key': 'server.port'})) == {
        'key': 'server.port', 'value': 50001, 'source': 'default'}
    assert data(await client.call_tool('get_config', {'key': 'server.auth'}))['value'] == '<not-secure>'
    assert 'database' in data(await client.call_tool('get_config', {}))['value']
    with pytest.raises(ToolError, match='no configuration value'):
        await client.call_tool('get_config', {'key': 'server.colour'})


async def test_resources_and_prompts(client, local_server):
    assert {str(resource.uri) for resource in await client.list_resources()} == {
        'hypershell://guide', 'hypershell://status', 'hypershell://config'}
    guide = (await client.read_resource('hypershell://guide'))[0].text
    assert guide.startswith('# Working with HyperShell') and '`select`' in guide
    created = await submit(client, ['hello'])
    task = json.loads((await client.read_resource(f"hypershell://task/{created['ids'][0]}"))[0].text)
    assert task['task']['args'] == 'hello'
    batch = json.loads((await client.read_resource(f"hypershell://batch/{created['batch']}"))[0].text)
    assert batch['counts']['pending'] == 1
    assert 'Usage:' in (await client.read_resource('hypershell://help/submit'))[0].text
    names = {prompt.name for prompt in await client.list_prompts()}
    assert names == {'parameter_sweep', 'triage_failures', 'campaign_report', 'resume_campaign'}
    prompt = await client.get_prompt('triage_failures', {'batch': 'b1'})
    assert 'diagnose_failures ({"batch": "b1"})' in prompt.messages[0].content.text


async def test_every_tool_has_one_tier_a_title_and_a_description(client, local_server):
    tiers = local_server.state.tool_tiers
    tools = await client.list_tools()
    assert {tool.name for tool in tools} == set(tiers) and len(tools) == 23
    for tool in tools:
        assert tool.title and tool.description and len(tool.description) > 80, tool.name
        assert tool.output_schema, tool.name
        assert tool.annotations.read_only_hint is (tiers[tool.name].value == 'read'), tool.name
    assert {tool.name for tool in tools if tool.annotations.destructive_hint} == {'delete_tasks', 'stop_cluster'}
