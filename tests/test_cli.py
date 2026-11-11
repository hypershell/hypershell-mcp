# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""The command line, run as a user would run it: as separate processes on their own site."""

import asyncio
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from conftest import data
from hypershell_mcp import auth

pytestmark = pytest.mark.integration

PROGRAM = [sys.executable, '-m', 'hypershell_mcp']


@pytest.fixture
def environment(tmp_path) -> dict[str, str]:
    """A site of its own for a child process, with nothing inherited from this session's."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith('HYPERSHELL_')}
    return {**kept, 'HYPERSHELL_SITE': str(tmp_path / 'site'), 'HYPERSHELL_CONFIG_FILE': str(tmp_path / 'config.toml'),
            'HYPERSHELL_DATABASE_FILE': str(tmp_path / 'task.db'), 'FASTMCP_CHECK_FOR_UPDATES': 'off'}


def run(environment, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([*PROGRAM, *arguments], env=environment, capture_output=True, text=True, timeout=60)


def test_keygen_writes_an_owner_only_secret_once(environment, tmp_path):
    target = tmp_path / 'keys' / 'mcp.secret'
    made = run(environment, 'keygen', str(target))
    assert made.returncode == 0 and f'--jwt-secret-file {target}' in made.stdout
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and len(target.read_text().strip()) >= 60
    again = run(environment, 'keygen', str(target))
    assert again.returncode == 3 and 'refusing to overwrite' in again.stderr


async def test_token_is_signed_with_the_configured_secret(environment, tmp_path):
    target = tmp_path / 'mcp.secret'
    run(environment, 'keygen', str(target))
    environment['HYPERSHELL_MCP_JWT_SECRETFILE'] = str(target)
    issued = run(environment, 'token', '--subject', 'ada', '--scopes', 'read,submit', '--ttl', '15m')
    assert issued.returncode == 0, issued.stderr
    access = await auth.build_verifier(auth.AuthSettings(secret=target.read_text().strip())).load_access_token(
        issued.stdout.strip())
    assert (access.subject, access.scopes) == ('ada', ['hypershell:read', 'hypershell:submit'])
    assert 890 < access.expires_at - access.claims['iat'] <= 900
    del environment['HYPERSHELL_MCP_JWT_SECRETFILE']
    missing = run(environment, 'token')
    assert missing.returncode == 3 and 'no signing secret' in missing.stderr
    assert run(environment, 'token', '--secret-file', str(target), '--scopes', 'root').returncode == 3


async def test_key_pair_tokens_verify_against_the_public_key(environment, tmp_path):
    target = tmp_path / 'mcp.key'
    made = run(environment, 'keygen', str(target), '--type', 'ec')
    assert made.returncode == 0 and stat.S_IMODE(target.stat().st_mode) == 0o600
    public = Path(str(target) + '.pub').read_text()
    issued = run(environment, 'token', '--private-key', str(target), '--scopes', 'all')
    assert issued.returncode == 0, issued.stderr
    verifier = auth.build_verifier(auth.AuthSettings(public_key=public, algorithm='ES256'))
    assert len((await verifier.load_access_token(issued.stdout.strip())).scopes) == 5


def test_check_shows_the_setup_and_refuses_what_serve_would(environment, tmp_path):
    shown = run(environment, 'check')
    assert shown.returncode == 0, shown.stderr
    assert 'Transport:  stdio' in shown.stdout and 'Auth:       none (local user)' in shown.stdout
    assert 'run     run_tasks, start_cluster, stop_cluster' in shown.stdout
    refused = run(environment, 'check', '--transport', 'http')
    assert refused.returncode == 3 and 'HTTP needs a JWT key' in refused.stderr
    exposed = run(environment, 'check', '--transport', 'http', '--no-auth', '--host', '0.0.0.0')
    assert exposed.returncode == 3 and 'without authentication' in exposed.stderr
    target = tmp_path / 'mcp.secret'
    run(environment, 'keygen', str(target))
    secured = run(environment, 'check', '-t', 'http', '-p', '9123', '--jwt-secret-file', str(target),
                  '--disable', 'run,admin')
    assert secured.returncode == 0, secured.stderr
    assert 'http://127.0.0.1:9123/mcp' in secured.stdout and 'JWT HS256' in secured.stdout
    assert secured.stdout.count('(disabled)') == 2
    assert run(environment, '--version').stdout.startswith('HyperShell MCP v')


async def test_served_over_stdio(environment):
    transport = StdioTransport(command=sys.executable, args=['-m', 'hypershell_mcp'], env=environment)
    async with Client(transport) as client:
        assert len(await client.list_tools()) == 23
        status = data(await client.call_tool('status', {}))
        assert status['access']['transport'] == 'stdio' and len(status['access']['tiers']) == 5
        created = data(await client.call_tool('submit_tasks', {'commands': ['echo over stdio']}))
        assert created['submitted'] == 1
        assert 'Usage:' in (await client.read_resource('hypershell://help/list'))[0].text


async def test_served_over_http_with_a_token(environment, tmp_path):
    target = tmp_path / 'mcp.secret'
    run(environment, 'keygen', str(target))
    token = run(environment, 'token', '--secret-file', str(target), '--scopes', 'read').stdout.strip()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen([*PROGRAM, '--transport', 'http', '--port', str(port), '--jwt-secret-file', str(target)],
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = f'http://127.0.0.1:{port}/mcp'
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                assert server.poll() is None, server.stderr.read().decode()
                await asyncio.sleep(0.1)
        async with Client(url, auth=token) as client:
            status = data(await client.call_tool('status', {}))
            assert status['access']['tiers'] == ['read'] and status['access']['subject']
            assert {tool.name for tool in await client.list_tools()} >= {'status', 'list_tasks'}
            assert 'submit_tasks' not in {tool.name for tool in await client.list_tools()}
        with pytest.raises(Exception):
            async with Client(url) as client:
                await client.list_tools()
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert server.stdout.read() == b''
