# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""The server as served over HTTP: bearer tokens, scopes, tiers, and the audit log."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client, asgi_server, run_server_async

from conftest import data
from hypershell_mcp import auth
from hypershell_mcp.server import build_server
from hypershell_mcp.settings import load_settings

pytestmark = pytest.mark.unit

READ = {'status', 'list_tasks', 'summarize_tasks', 'get_task', 'get_task_output', 'diagnose_failures',
        'wait_for_tasks', 'list_tags', 'get_config', 'list_clusters'}
MANAGE = {'cancel_tasks', 'retry_tasks', 'tag_tasks', 'delete_tasks'}
RUN = {'run_tasks', 'start_cluster', 'stop_cluster'}
ADMIN = {'init_database', 'vacuum_database', 'backup_database', 'rotate_database', 'set_config'}


async def tool_names(server, bearer) -> set[str]:
    async with asgi_client(server, auth=bearer) as client:
        return {tool.name for tool in await client.list_tools()}


async def test_requests_without_a_valid_token_are_refused(secured_server, token):
    async with asgi_server(secured_server) as served:
        async with served.http_client() as http:
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
            assert (await http.post('/mcp', json=body)).status_code == 401
            for bad in ('not-a-token', token('all', ttl=-60),
                        auth.mint_token(auth.generate_secret(), subject='x', scopes=['hypershell:admin'], ttl=60),
                        token('all', audience='elsewhere')):
                response = await http.post('/mcp', json=body, headers={'Authorization': f'Bearer {bad}'})
                assert response.status_code == 401, bad
                assert response.headers['www-authenticate'].startswith('Bearer')


async def test_health_is_open_and_says_nothing_of_the_site(secured_server):
    async with asgi_server(secured_server) as served:
        async with served.http_client() as http:
            response = await http.get('/health')
            assert response.status_code == 200
            assert set(response.json()) == {'status', 'server', 'version'}


async def test_scopes_decide_which_tools_exist_for_a_caller(secured_server, token):
    assert await tool_names(secured_server, token('read')) == READ
    assert await tool_names(secured_server, token('read', 'submit')) == READ | {'submit_tasks'}
    assert await tool_names(secured_server, token('read', 'manage')) == READ | MANAGE
    assert await tool_names(secured_server, token('all')) == READ | {'submit_tasks'} | MANAGE | RUN | ADMIN
    # A token for something else entirely authenticates but can do nothing here.
    assert await tool_names(secured_server, auth.mint_token(
        auth_secret(secured_server), subject='x', scopes=['other:thing'], ttl=60)) == set()


def auth_secret(server) -> str:
    return server.state.settings.auth.secret


async def test_calling_beyond_your_scopes_names_the_missing_scope(secured_server, token):
    async with asgi_client(secured_server, auth=token('read')) as client:
        assert data(await client.call_tool('status', {}))['access']['tiers'] == ['read']
        with pytest.raises(ToolError, match='hypershell:submit'):
            await client.call_tool('submit_tasks', {'commands': ['echo no']})
        with pytest.raises(ToolError, match='hypershell:run'):
            await client.call_tool('start_cluster', {})
        assert data(await client.call_tool('status', {}))['tasks']['total'] == 0


async def test_caller_identity_reaches_tasks_and_the_audit_log(secured_server, token, site):
    audit = secured_server.state.state_dir / 'audit.jsonl'
    audit.unlink(missing_ok=True)
    async with asgi_client(secured_server, auth=token('read', 'submit', subject='ada@lab')) as client:
        status = data(await client.call_tool('status', {}))
        assert status['access']['subject'] == 'ada@lab' and status['access']['token_expires']
        assert status['access']['transport'] == 'streamable-http'
        created = data(await client.call_tool('submit_tasks', {'commands': ['echo hi']}))
        (task,) = data(await client.call_tool('list_tasks', {'fields': ['tag']}))['tasks']
        assert task['tag'] == {'batch': created['batch'], 'submitter': 'ada_lab'}
        with pytest.raises(ToolError):
            await client.call_tool('delete_tasks', {'select': {'batch': created['batch']}, 'confirm': True})
        with pytest.raises(ToolError, match='one line'):
            await client.call_tool('submit_tasks', {'commands': ['a\nb']})
    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [(entry['tool'], entry['tier'], entry['ok'], entry['subject']) for entry in entries] == [
        ('submit_tasks', 'submit', True, 'ada@lab'),
        ('delete_tasks', 'manage', False, 'ada@lab'),
        ('submit_tasks', 'submit', False, 'ada@lab')]
    assert 'echo hi' in entries[0]['arguments'] and 'scope' in entries[1]['error']
    assert not any(entry['tool'] in READ for entry in entries)


async def test_disabled_tiers_are_gone_whatever_the_token(secret, token):
    server = build_server(load_settings({'disable': ['run', 'admin']}, transport='http', secret=secret))
    assert await tool_names(server, token('all')) == READ | {'submit_tasks'} | MANAGE
    readonly = build_server(load_settings({}, transport='http', secret=secret, readonly=True))
    assert await tool_names(readonly, token('all')) == READ
    async with asgi_client(readonly, auth=token('all')) as client:
        assert data(await client.call_tool('status', {}))['access']['tiers'] == ['read']
        with pytest.raises(ToolError):
            await client.call_tool('submit_tasks', {'commands': ['echo no']})


async def test_resources_and_prompts_need_the_read_scope(secured_server, token):
    other = auth.mint_token(auth_secret(secured_server), subject='x', scopes=['hypershell:submit'], ttl=60)
    async with asgi_client(secured_server, auth=other) as client:
        assert await client.list_resources() == [] and await client.list_prompts() == []
    async with asgi_client(secured_server, auth=token('read')) as client:
        assert len(await client.list_resources()) == 3 and len(await client.list_prompts()) == 4


async def test_public_key_server_over_a_real_socket():
    """The asymmetric setup, end to end over TCP: the server holds only the public key."""
    private, public, algorithm = auth.generate_keypair('ec')
    server = build_server(load_settings({}, transport='http', pubkey=public, algorithm=algorithm))
    bearer = auth.mint_token(private, algorithm=algorithm, subject='remote', scopes=auth.parse_scopes('read'), ttl=60)
    async with run_server_async(server, transport='http', path='/mcp') as url:
        async with Client(url, auth=bearer) as client:
            status = data(await client.call_tool('status', {}))
            assert status['access'] == {'subject': 'remote', 'transport': 'streamable-http', 'tiers': ['read'],
                                        'token_expires': status['access']['token_expires']}
        with pytest.raises(Exception):
            async with Client(url, auth='garbage') as client:
                await client.list_tools()
