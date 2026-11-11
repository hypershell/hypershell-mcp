# SPDX-FileCopyrightText: 2025 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Test fixtures and configuration."""

import os
from pathlib import Path

from pytest import fixture

# Load .env file if present (for local development)
# This allows sharing configuration with the parent hypershell project
try:
    from dotenv import load_dotenv
    
    # Check for .env in current directory first, then parent hypershell project
    env_paths = [
        Path(__file__).parent.parent / ".env",
        Path(__file__).parent.parent.parent / "hypershell" / ".env",
    ]
    
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass  # python-dotenv not installed, skip


@fixture(scope="function")
def temp_site(tmpdir_factory) -> Path:
    """Setup empty site and database path for isolated testing."""
    # Clear any existing HYPERSHELL_ environment variables
    for name in list(os.environ.keys()):
        if name.startswith('HYPERSHELL_'):
            os.environ.pop(name)
    
    site = tmpdir_factory.mktemp('data')
    db_path = site.join('local.db')
    
    os.environ['HYPERSHELL_CONFIG_FILE'] = ''
    os.environ['HYPERSHELL_SITE'] = str(site)
    os.environ['HYPERSHELL_DATABASE_FILE'] = str(db_path)
    os.environ['HYPERSHELL_LOGGING_LEVEL'] = 'DEBUG'
    
    return Path(str(site))
