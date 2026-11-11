# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Server settings.

The server is configured the way HyperShell is: an ``[mcp]`` section in the HyperShell
configuration files, ``HYPERSHELL_MCP_*`` environment variables, and command-line options, each
overriding the one before. Key names are single words so that they map cleanly onto environment
variables (``mcp.jwt.secret`` is ``HYPERSHELL_MCP_JWT_SECRET``).

.. code-block:: toml

    [mcp]
    transport = "http"
    host = "127.0.0.1"
    port = 8000
    disable = ["admin"]

    [mcp.jwt]
    secretfile = "~/.config/hypershell-mcp/secret"
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Mapping

from hypershell_mcp.auth import (DEFAULT_AUDIENCE, DEFAULT_ISSUER, TIERS, AuthError, AuthSettings,
                                 Tier, read_key_file)

__all__ = ['Settings', 'Limits', 'SettingsError', 'load_settings', 'DEFAULT_HOST', 'DEFAULT_PORT',
           'DEFAULT_PATH', 'TRANSPORTS']

DEFAULT_HOST: Final[str] = '127.0.0.1'
DEFAULT_PORT: Final[int] = 8000
DEFAULT_PATH: Final[str] = '/mcp'
TRANSPORTS: Final[tuple[str, ...]] = ('stdio', 'http')
LOOPBACK: Final[frozenset[str]] = frozenset({'127.0.0.1', 'localhost', '::1'})


class SettingsError(Exception):
    """The settings cannot be used; the message says what to change."""


@dataclass(frozen=True)
class Limits:
    """Ceilings that keep one call from flooding the model's context or the database."""

    rows: int = 500
    """Most tasks one listing call returns."""

    output: int = 32_768
    """Most bytes of task output one call returns."""

    submit: int = 10_000
    """Most tasks one submit call accepts."""

    wait: int = 600
    """Longest one wait call blocks, in seconds."""


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to start."""

    transport: str = 'stdio'
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    auth: AuthSettings = field(default_factory=AuthSettings)
    anonymous: bool = False
    """Serve HTTP with no authentication at all. Loopback only; for development."""

    tiers: frozenset[Tier] = frozenset(TIERS)
    """The tiers this server offers. A tier left out is gone for every caller, whatever its token."""

    limits: Limits = field(default_factory=Limits)

    hosts: tuple[str, ...] = ()
    """
    Host names requests may be addressed to, beyond the address the server is bound to. Needed
    behind a reverse proxy on the same machine that passes the public Host header through.
    """

    @property
    def readonly(self) -> bool:
        return self.tiers == frozenset({Tier.READ})

    def validate(self) -> None:
        """Raise `SettingsError` for a combination that must not be served."""
        if self.transport not in TRANSPORTS:
            raise SettingsError(f'unknown transport {self.transport!r}: use stdio or http')
        if self.transport != 'http':
            return
        if not self.path.startswith('/'):
            raise SettingsError(f'the HTTP path must start with a slash, not {self.path!r}')
        if self.anonymous:
            if self.auth.enabled:
                raise SettingsError('--no-auth and a JWT key were both given; choose one')
            if self.host not in LOOPBACK:
                raise SettingsError(
                    f'refusing to serve {self.host} without authentication: this server runs shell '
                    'commands. Give a JWT key, or bind to 127.0.0.1')
            return
        if not self.auth.enabled:
            raise SettingsError(
                "HTTP needs a JWT key: make one with 'hypershell-mcp keygen', then pass "
                "--jwt-secret-file (or set mcp.jwt.secretfile). For local development only, "
                '--no-auth serves 127.0.0.1 without it')
        try:
            self.auth.validate()
        except AuthError as error:
            raise SettingsError(str(error)) from None


def parse_tiers(names: Any, what: str) -> frozenset[Tier]:
    """Tiers from a list or a comma-separated string."""
    if names is None:
        return frozenset()
    if isinstance(names, str):
        names = names.replace(',', ' ').split()
    try:
        return frozenset(Tier(str(name)) for name in names)
    except ValueError as error:
        known = ', '.join(tier.value for tier in TIERS)
        raise SettingsError(f'{what}: {error}; the tiers are {known}') from None


def clean(value: Any) -> Any:
    """Treat HyperShell's ``<none>`` convention and empty strings as unset."""
    return None if value in (None, '', '<none>') else value


def load_settings(section: Mapping[str, Any] | None = None, **overrides: Any) -> Settings:
    """
    Build settings from the ``[mcp]`` configuration section and command-line `overrides`.

    An override of None means "not given on the command line" and leaves the configured value.
    Recognised overrides: transport, host, port, path, anonymous, readonly, disable, secret,
    secretfile, pubkey, jwks, algorithm, issuer, audience.
    """
    section = dict(section or {})
    jwt = dict(section.get('jwt') or {})
    limit = dict(section.get('limit') or {})

    def pick(name: str, source: Mapping[str, Any], default: Any = None) -> Any:
        given = clean(overrides.get(name))
        return given if given is not None else clean(source.get(name, default)) or default

    secret = pick('secret', jwt)
    secretfile = pick('secretfile', jwt)
    pubkey = pick('pubkey', jwt)
    try:
        if secretfile and not clean(overrides.get('secret')):
            secret = read_key_file(Path(str(secretfile)))
        if pubkey and '-----BEGIN' not in str(pubkey):
            pubkey = read_key_file(Path(str(pubkey)))
    except AuthError as error:
        raise SettingsError(str(error)) from None

    issuer = overrides.get('issuer') if overrides.get('issuer') is not None else jwt.get('issuer', DEFAULT_ISSUER)
    audience = (overrides.get('audience') if overrides.get('audience') is not None
                else jwt.get('audience', DEFAULT_AUDIENCE))
    auth = AuthSettings(secret=secret, public_key=pubkey, jwks_uri=pick('jwks', jwt),
                        algorithm=pick('algorithm', jwt), issuer=clean(issuer), audience=clean(audience))

    tiers = frozenset(TIERS) - parse_tiers(overrides.get('disable') or section.get('disable'), 'mcp.disable')
    if overrides.get('readonly') or (overrides.get('readonly') is None and section.get('readonly')):
        tiers = frozenset({Tier.READ})
    if Tier.READ not in tiers:
        raise SettingsError('the read tier cannot be disabled: every other tier builds on it')

    hosts = section.get('hosts') or []
    if isinstance(hosts, str):
        hosts = hosts.replace(',', ' ').split()
    defaults = Limits()
    try:
        limits = replace(defaults, **{name: int(limit[name]) for name in ('rows', 'output', 'submit', 'wait')
                                      if clean(limit.get(name)) is not None})
        port = int(pick('port', section, DEFAULT_PORT))
    except (TypeError, ValueError) as error:
        raise SettingsError(f'a numeric mcp setting is not a number: {error}') from None
    if min(limits.rows, limits.output, limits.submit, limits.wait) <= 0:
        raise SettingsError('every mcp.limit value must be positive')

    settings = Settings(transport=str(pick('transport', section, 'stdio')),
                        host=str(pick('host', section, DEFAULT_HOST)), port=port,
                        path=str(pick('path', section, DEFAULT_PATH)), auth=auth,
                        anonymous=bool(overrides.get('anonymous')), tiers=tiers, limits=limits,
                        hosts=tuple(str(name) for name in hosts))
    settings.validate()
    return settings
