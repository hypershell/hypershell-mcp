# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Running real work: clusters, output, failure diagnosis and database upkeep, end to end."""

import asyncio
import json
import os

import pytest
from fastmcp.exceptions import ToolError

from conftest import data

pytestmark = pytest.mark.integration


async def run(client, commands, **options) -> dict:
    return data(await client.call_tool('run_tasks', {'commands': commands, 'timeout': 90, **options}))


async def cluster_exited(client, cluster_id: str, seconds: float = 30) -> dict:
    for _ in range(int(seconds * 4)):
        (cluster,) = data(await client.call_tool('list_clusters', {'cluster_id': cluster_id, 'log_lines': 20}))['clusters']
        if cluster['state'] == 'exited':
            return cluster
        await asyncio.sleep(0.25)
    raise AssertionError(f'cluster {cluster_id} is still running after {seconds} s')


async def test_run_tasks_returns_each_result_in_order(client, monkeypatch):
    monkeypatch.setenv('HYPERSHELL_MCP_JWT_SECRET', 'must-not-reach-a-task')
    result = await run(client, [
        'echo one',
        "echo 'kept # as written'",
        'echo to-stderr >&2; exit 4',
        'printenv HYPERSHELL_MCP_JWT_SECRET || echo absent',
        {'command': 'echo $TASK_TAG_ALPHA', 'tags': {'alpha': 0.5}},
    ], workers=3, tags={'sweep': 't1'})
    assert result['done'] is True
    assert result['counts'] == {'pending': 0, 'running': 0, 'succeeded': 4, 'failed': 1, 'cancelled': 0, 'total': 5}
    outcomes = [(item['status'], item['exit_status'], (item['stdout'] or '').strip(), (item['stderr'] or '').strip())
                for item in result['results']]
    assert outcomes == [('succeeded', 0, 'one', ''), ('succeeded', 0, 'kept # as written', ''),
                        ('failed', 4, '', 'to-stderr'), ('succeeded', 0, 'absent', ''), ('succeeded', 0, '0.5', '')]
    cluster = await cluster_exited(client, result['cluster'])
    assert cluster['exit_code'] == 0 and cluster['mode'] == 'drain' and cluster['workers'] == 3
    assert cluster['command'].startswith('hs cluster --restart --capture -N 3')
    assert any('Running task' in line for line in cluster['log_tail'])


async def test_output_can_be_read_by_head_tail_and_pattern(client):
    result = await run(client, ["for i in $(seq 1 200); do echo line $i; done; echo 'ERROR: disk 7 full' >&2"],
                       output_lines=3)
    (task,) = result['results']
    assert task['stdout'].splitlines() == ['line 198', 'line 199', 'line 200'] and task['truncated'] is True

    async def output(**arguments):
        return data(await client.call_tool('get_task_output', {'task_id': task['id'], **arguments}))

    head = await output(mode='head', lines=2)
    assert head['text'] == 'line 1\nline 2\n' and head['truncated'] is True and head['size'] > 1000
    assert (await output(lines=1))['text'] == 'line 200\n'
    found = await output(pattern=r'line 1?9$')
    assert found['text'] == '9: line 9\n19: line 19' and found['matches'] == 2
    assert (await output(stream='stderr'))['text'] == 'ERROR: disk 7 full\n'
    assert 'not a valid regular expression' in (await output(pattern='('))['reason']
    detail = data(await client.call_tool('get_task', {'task_id': task['id'], 'output_lines': 2}))
    assert detail['stdout']['text'] == 'line 199\nline 200\n' and detail['task']['command'].startswith('for i in')
    resource = await client.read_resource(f"hypershell://task/{task['id']}/stderr")
    assert resource[0].text == 'ERROR: disk 7 full\n'


async def test_output_outside_the_task_directory_is_never_read(client, tmp_path):
    result = await run(client, ['echo hi'])
    task_id = result['results'][0]['id']
    secret_file = tmp_path / 'private.txt'
    secret_file.write_text('do not read me')
    from hypershell.data.core import Session
    from hypershell.data.model import Task
    Task.update(task_id, outpath=str(secret_file))
    Session.remove()
    chunk = data(await client.call_tool('get_task_output', {'task_id': task_id}))
    assert chunk['available'] is False and chunk['text'] == '' and 'outside the directories' in chunk['reason']


async def test_failures_are_grouped_by_cause(client):
    commands = ([f"echo 'ValueError: bad sample {n} in /data/run{n}/in.csv' >&2; exit 1" for n in range(4)]
                + ['nosuchcommand --flag', 'nosuchcommand --other']
                + ['kill -9 $$', 'echo fine'])
    result = await run(client, commands, workers=4)
    assert result['counts']['failed'] == 7
    report = data(await client.call_tool('diagnose_failures', {'select': {'batch': result['batch']}}))
    assert (report['failed'], report['examined']) == (7, 7)
    clusters = {(cluster['exit_status'], cluster['count']): cluster for cluster in report['clusters']}
    assert set(clusters) == {(1, 4), (127, 2), (-9, 1)}
    assert clusters[(1, 4)]['signature'] == 'ValueError: bad sample <n> in <path>'
    assert 'ValueError: bad sample' in clusters[(1, 4)]['example_stderr'] and len(clusters[(1, 4)]['example_ids']) == 4
    assert clusters[(127, 2)]['meaning'] == 'command not found'
    assert clusters[(-9, 1)]['meaning'] == 'killed by SIGKILL'
    assert any('127' in line for line in report['advice'])
    summary = data(await client.call_tool('summarize_tasks', {'select': {'batch': result['batch']},
                                                               'group_by': ['exit_status']}))
    assert {row['key']['exit_status']: row['count'] for row in summary['rows']} == {1: 4, 127: 2, -9: 1, 0: 1}


async def test_fix_then_retry(client, tmp_path):
    marker = tmp_path / 'ready'
    result = await run(client, [f'test -e {marker} && echo ok'])
    assert result['counts']['failed'] == 1
    marker.touch()
    retried = data(await client.call_tool('retry_tasks', {'select': {'batch': result['batch']}}))
    assert retried['changed'] == 1
    # Straight away, while the cluster that ran the first attempt may still be winding down: a bare
    # start_cluster returns whichever cluster ends up serving the queue, that one or a new one.
    serving = data(await client.call_tool('start_cluster', {}))
    waited = data(await client.call_tool('wait_for_tasks', {'select': {'batch': result['batch']}, 'timeout': 60}))
    assert waited['done'] is True and waited['counts']['succeeded'] == 1
    await cluster_exited(client, serving['id'])


async def test_forever_cluster_serves_new_submissions_until_drained(client):
    started = data(await client.call_tool('start_cluster', {'options': {'mode': 'forever', 'workers': 2}}))
    assert started['state'] == 'running' and '--forever' in started['command']
    # A bare call asks only that the queue be served, which it is; asking for a different
    # cluster while one runs is refused.
    assert data(await client.call_tool('start_cluster', {}))['id'] == started['id']
    with pytest.raises(ToolError, match='already running'):
        await client.call_tool('start_cluster', {'options': {'workers': 8}})
    status = data(await client.call_tool('status', {}))
    assert [cluster['id'] for cluster in status['clusters']] == [started['id']]

    for round_ in range(2):
        batch = data(await client.call_tool('submit_tasks', {'commands': [f'echo round {round_}']}))['batch']
        waited = data(await client.call_tool('wait_for_tasks', {'select': {'batch': batch}, 'timeout': 60}))
        assert waited['done'] is True and waited['counts']['succeeded'] == 1

    stopping = data(await client.call_tool('stop_cluster', {'how': 'drain'}))
    assert stopping['id'] == started['id']
    cluster = await cluster_exited(client, started['id'])
    assert cluster['exit_code'] == 0 and cluster['stopped']
    assert 'SIGUSR1' in (await client.read_resource(f"hypershell://cluster/{started['id']}/log"))[0].text
    with pytest.raises(ToolError, match='No cluster started by this server is running'):
        await client.call_tool('stop_cluster', {})
    assert data(await client.call_tool('stop_cluster', {'cluster_id': started['id']}))['state'] == 'exited'


async def test_kill_stops_running_tasks_and_the_next_cluster_requeues_them(client):
    started = data(await client.call_tool('start_cluster', {'options': {'mode': 'forever', 'workers': 1}}))
    batch = data(await client.call_tool('submit_tasks', {'commands': ['sleep 300']}))['batch']
    for _ in range(80):
        if data(await client.call_tool('status', {}))['tasks']['running'] == 1:
            break
        await asyncio.sleep(0.25)
    killed = data(await client.call_tool('stop_cluster', {'how': 'kill'}))
    assert killed['state'] == 'exited'
    status = data(await client.call_tool('status', {}))
    assert status['tasks']['running'] == 1 and status['clusters'] == []
    assert any('interrupted' in hint for hint in status['hints'])
    cancelled = data(await client.call_tool('cancel_tasks', {'select': {'batch': batch}}))
    assert cancelled['changed'] == 1


async def test_bad_cluster_options_are_refused_before_anything_starts(client, tmp_path):
    for options, message in (({'launcher': 'ssh'}, 'needs hosts or ssh_group'),
                             ({'launcher': 'custom'}, 'needs launcher_command'),
                             ({'autoscaling': 'dynamic'}, "needs launcher 'custom'"),
                             ({'cwd': str(tmp_path / 'missing')}, 'does not exist')):
        with pytest.raises(ToolError, match=message):
            await client.call_tool('start_cluster', {'options': options})
    assert data(await client.call_tool('list_clusters', {}))['clusters'] == [] or all(
        cluster['state'] == 'exited' for cluster in data(await client.call_tool('list_clusters', {}))['clusters'])


def test_cluster_options_become_the_documented_hs_arguments():
    from hypershell_mcp.clusters import ClusterOptions
    arguments = ClusterOptions(
        mode='forever', workers=16, max_retries=2, eager=True, task_timeout=3600, launcher='custom',
        launcher_command='srun --exclusive -N1', autoscaling='dynamic', init_size=2, min_size=1, max_size=40,
        zone='gpu', template='python run.py {}', bundlesize=10, client_cores=64, client_memory='128GB',
        client_timeout=600, monitor=True).arguments()
    assert arguments == ['cluster', '--forever', '--capture', '-N', '16', '-Q', '2', '-r', '2', '--eager',
                         '-W', '3600', '--template=python run.py {}', '-b', '10', '-C', '64',
                         '--client-memory=128GB', '-T', '600', '--monitor', '--zone=gpu',
                         '--launcher=srun --exclusive -N1', '--autoscaling', 'dynamic', '-I', '2', '-X', '1',
                         '-Y', '40']
    assert ClusterOptions(launcher='ssh', hosts=['a', 'b']).arguments()[-1] == '--ssh=a,b'
    assert ClusterOptions(launcher='ssh', hosts=['node[1-4]']).arguments()[-1] == '--ssh=node[1-4]'
    assert ClusterOptions(launcher='ssh', ssh_group='gpu').arguments()[-2:] == ['--ssh', '--ssh-group=gpu']
    assert ClusterOptions(launcher='mpi').arguments()[-1] == '--mpi'


async def test_a_cluster_with_a_zone_and_a_client_template_runs_only_its_tasks(client):
    """The free-text options reach hs intact, and behave as documented."""
    theirs = data(await client.call_tool('submit_tasks', {'commands': ['alpha beta'], 'zone': 'lab-1'}))
    other = data(await client.call_tool('submit_tasks', {'commands': ['echo not for this cluster']}))
    started = data(await client.call_tool('start_cluster', {'options': {
        'mode': 'forever', 'workers': 1, 'zone': 'lab-1', 'template': "echo 'got: {}' # literal"}}))
    assert "--zone=lab-1" in started['command'] and started['zone'] == 'lab-1'
    waited = data(await client.call_tool('wait_for_tasks', {'select': {'batch': theirs['batch']}, 'timeout': 60}))
    assert waited['done'] is True and waited['counts']['succeeded'] == 1
    output = data(await client.call_tool('get_task_output', {'task_id': theirs['ids'][0]}))
    assert output['text'] == 'got: alpha beta\n'
    # The task without a zone was not run: this cluster's client serves lab-1 only. (The scheduler
    # has staged it for a default-zone client, which HyperShell records as scheduled.)
    left = data(await client.call_tool('summarize_tasks', {'select': {'batch': other['batch']}}))
    assert [(row['key']['status'], row['count']) for row in left['rows']] in ([('running', 1)], [('pending', 1)])
    await client.call_tool('stop_cluster', {'how': 'kill'})


async def test_database_upkeep(client, site):
    result = await run(client, ['echo a', 'echo b', 'exit 1'])
    await cluster_exited(client, result['cluster'])

    backup = data(await client.call_tool('backup_database', {'name': 'before.db'}))
    assert backup['ok'] is True and backup['path'].endswith('mcp/backups/before.db') and os.path.getsize(backup['path']) > 0
    with pytest.raises(ToolError, match='already exists'):
        await client.call_tool('backup_database', {'name': 'before.db'})
    with pytest.raises(ToolError, match='plain file name'):
        await client.call_tool('backup_database', {'name': '../../escape.db'})

    assert data(await client.call_tool('init_database', {}))['ok'] is True
    assert data(await client.call_tool('vacuum_database', {}))['ok'] is True
    assert data(await client.call_tool('status', {}))['tasks']['total'] == 3

    rotated = data(await client.call_tool('rotate_database', {}))
    assert rotated['ok'] is True, rotated['detail']
    status = data(await client.call_tool('status', {}))
    assert status['tasks']['total'] == 0 and status['database']['partitions'] == 1

    pending = data(await client.call_tool('submit_tasks', {'commands': ['sleep 30']}))
    started = data(await client.call_tool('start_cluster', {'options': {'workers': 1}}))
    with pytest.raises(ToolError, match='while tasks are running or a cluster is up'):
        await client.call_tool('vacuum_database', {})
    await client.call_tool('stop_cluster', {'cluster_id': started['id'], 'how': 'kill'})
    await client.call_tool('delete_tasks', {'select': {'batch': pending['batch']}, 'confirm': True})


async def test_set_config_writes_the_site_file_and_protects_its_own_section(client, site):
    written = data(await client.call_tool('set_config', {'key': 'task.timeout', 'value': 120}))
    assert written['ok'] is True and written['path'] == str(site / 'config.toml'), written
    assert 'timeout = 120' in (site / 'config.toml').read_text()
    (site / 'config.toml').unlink()
    for key in ('mcp.jwt.secret', 'timeout', 'database.file', 'export.PATH', 'server.auth', 'task.cwd',
                'autoscale.launcher', 'ssh.nodelist', 'task.timeout_eval', 'database.x_eval'):
        with pytest.raises(ToolError, match='not settable through this server'):
            await client.call_tool('set_config', {'key': key, 'value': 'x'})
    assert not (site / 'config.toml').exists()
