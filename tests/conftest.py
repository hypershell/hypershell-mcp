# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Test fixtures.

HyperShell loads its configuration once, when it is first imported, so the whole test session runs
against one throwaway site chosen here before anything imports it. Each test starts from an empty
database.
"""

import os
import shutil
import tempfile
from pathlib import Path

for _name in [name for name in os.environ if name.startswith('HYPERSHELL_')]:
    del os.environ[_name]

SITE = Path(tempfile.mkdtemp(prefix='hsmcp-'))
os.environ['HYPERSHELL_SITE'] = str(SITE)
os.environ['HYPERSHELL_CONFIG_FILE'] = str(SITE / 'config.toml')  # only this file is read
os.environ['HYPERSHELL_DATABASE_FILE'] = str(SITE / 'task.db')
os.environ['FASTMCP_LOG_LEVEL'] = 'ERROR'
os.environ['FASTMCP_CHECK_FOR_UPDATES'] = 'off'

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastmcp import Client  # noqa: E402

from hypershell_mcp import auth  # noqa: E402
from hypershell_mcp.server import build_server  # noqa: E402
from hypershell_mcp.settings import load_settings  # noqa: E402

SECRET = auth.generate_secret()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(SITE, ignore_errors=True)


@pytest.fixture
def site() -> Path:
    return SITE


@pytest.fixture(autouse=True)
def empty_database():
    """Start every test with no tasks and no leftover clusters."""
    from hypershell.data import ensuredb
    from hypershell.data.core import Session
    from hypershell.data.model import Task
    ensuredb()
    Session.query(Task).delete()
    Session.commit()
    Session.remove()
    yield
    from hypershell_mcp.clusters import ClusterManager
    manager = ClusterManager(SITE / 'Library' / 'mcp' / 'clusters')
    for folder in (SITE / 'Library', SITE / 'lib'):
        manager.root = folder / 'mcp' / 'clusters'
        for cluster in manager.running():
            manager.stop(cluster.id, 'kill')


@pytest.fixture
def secret() -> str:
    return SECRET


@pytest.fixture
def token():
    """Make a token for the test secret: token('read', 'submit')."""

    def make(*tiers: str, ttl: int = 300, subject: str = 'tester', **options) -> str:
        return auth.mint_token(SECRET, subject=subject, scopes=auth.parse_scopes(' '.join(tiers) or 'read'),
                               ttl=ttl, **options)

    return make


@pytest.fixture
def local_server():
    """A server as a local user gets it over stdio: no authentication, every tier."""
    return build_server(load_settings({}))


@pytest.fixture
def secured_server():
    """A server as served over HTTP with a shared secret."""
    return build_server(load_settings({}, transport='http', secret=SECRET))


@pytest_asyncio.fixture
async def client(local_server):
    async with Client(local_server) as connected:
        yield connected


def data(result):
    """The structured result of a tool call."""
    return result.structured_content
