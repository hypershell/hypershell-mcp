# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
The ``hypershell-mcp`` command.

With no command it serves, so an MCP client can be pointed at the bare program. The other commands
are what an operator needs around a served instance: make a key, sign a token, check the setup.

HyperShell reads its configuration when it is imported, so nothing here imports it until the
options that choose the site have been applied.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from platform import python_implementation, python_version
from typing import Any, Final

from cmdkit.app import Application, exit_status
from cmdkit.cli import Interface

from hypershell_mcp import __version__

__all__ = ['main', 'ServeApp', 'TokenApp', 'KeygenApp', 'CheckApp']

PROGRAM: Final[str] = 'hypershell-mcp'
VERSION: Final[str] = f'HyperShell MCP v{__version__} ({python_implementation()} {python_version()})'


def fail(error: Exception) -> int:
    """Print a one-line reason and return the status HyperShell uses for bad configuration."""
    print(f'{PROGRAM}: {error}', file=sys.stderr)
    return exit_status.bad_config


def choose_site(site: str | None) -> None:
    """Point HyperShell at a site directory before it is imported."""
    if site:
        os.environ['HYPERSHELL_SITE'] = str(Path(site).expanduser().resolve())


def configured() -> dict[str, Any]:
    """
    The ``[mcp]`` section of the HyperShell configuration.

    HyperShell's configuration lets a value be given indirectly: ``secret_env = "NAME"`` reads an
    environment variable and ``secret_eval = "command"`` runs a command, both resolved when the
    plain name is read as an attribute. The jwt section is read that way here, so a secret need
    not sit in the file.
    """
    from hypershell_mcp.backend import lib
    section = lib().config.get('mcp')
    if not section:
        return {}
    found = dict(section)
    jwt = section.get('jwt')
    if jwt:
        from hypershell_mcp.clusters import protect
        resolved = {}
        for name in jwt:
            base = name.rsplit('_', 1)[0] if name.endswith(('_env', '_eval')) else name
            resolved[base] = getattr(jwt, base)
            if name.endswith('_env'):
                protect(str(jwt[name]))  # the variable holds a secret: tasks must not inherit it
        found['jwt'] = resolved
    return found


def handled() -> dict[type[Exception], Any]:
    from hypershell_mcp.auth import AuthError
    from hypershell_mcp.backend import BackendError
    from hypershell_mcp.settings import SettingsError
    return {AuthError: fail, BackendError: fail, SettingsError: fail}


# -------------------------------------------------------------------------------------------------

SERVE_USAGE: Final[str] = f"""\
Usage:
  {PROGRAM} [serve] [-h] [-t TRANSPORT] [-H ADDR] [-p PORT] [--path PATH] [--site DIR]
            [--jwt-secret-file PATH | --jwt-public-key PATH | --jwks-uri URL | --no-auth]
            [--jwt-algorithm ALG] [--jwt-issuer NAME] [--jwt-audience NAME]
            [--read-only | --disable TIER[,TIER...]]

  Serve HyperShell to MCP clients.\
"""

SERVE_HELP: Final[str] = f"""\
{SERVE_USAGE}

  The server acts on one HyperShell site: the one this environment selects, as for `hs`.
  Over stdio (the default) it is a local tool for the user who started it. Over HTTP every
  request must carry a JWT bearer token, and the token's scopes decide which tiers of tools
  the caller gets: read, submit, manage, run, admin.

  Options override the [mcp] section of the HyperShell configuration, which overrides nothing
  else: `{PROGRAM} check` shows what would be served.

Options:
  -t, --transport       NAME   Transport: stdio or http (default: stdio).
  -H, --host            ADDR   Bind address for HTTP (default: 127.0.0.1).
  -p, --port            PORT   Port number for HTTP (default: 8000).
      --path            PATH   URL path of the MCP endpoint (default: /mcp).
      --site            DIR    HyperShell site directory (default: from the environment).
      --jwt-secret-file PATH   File holding the shared secret that signs tokens (HS256).
      --jwt-public-key  PATH   PEM public key that verifies tokens (RS256, ES256, ...).
      --jwks-uri        URL    JWKS endpoint of an identity provider.
      --jwt-algorithm   ALG    Signature algorithm (default: HS256 or RS256 by key type).
      --jwt-issuer      NAME   Required issuer claim (default: hypershell-mcp; "" for any).
      --jwt-audience    NAME   Required audience claim (default: hypershell-mcp; "" for any).
      --no-auth                Serve HTTP without authentication. Loopback only.
      --read-only              Offer only the read tier.
      --disable         TIERS  Tiers to switch off for every caller, e.g. run,admin.
  -v, --version                Show the version and exit.
  -h, --help                   Show this message and exit.

Commands:
  serve                        Serve (the default when no command is given).
  token                        Sign a bearer token.
  keygen                       Make a signing secret or key pair.
  check                        Show the effective setup without serving.\
"""


def serve_interface(name: str, usage: str, help_text: str) -> Interface:
    """The options that decide what is served, shared by `serve` and `check`."""
    interface = Interface(name, usage, help_text)
    interface.add_argument('-v', '--version', action='version', version=VERSION)
    interface.add_argument('-t', '--transport', default=None, choices=['stdio', 'http'])
    interface.add_argument('-H', '--host', default=None)
    interface.add_argument('-p', '--port', type=int, default=None)
    interface.add_argument('--path', default=None)
    interface.add_argument('--site', default=None)
    key = interface.add_mutually_exclusive_group()
    key.add_argument('--jwt-secret-file', dest='secretfile', default=None)
    key.add_argument('--jwt-public-key', dest='pubkey', default=None)
    key.add_argument('--jwks-uri', dest='jwks', default=None)
    key.add_argument('--no-auth', dest='anonymous', action='store_true')
    interface.add_argument('--jwt-algorithm', dest='algorithm', default=None)
    interface.add_argument('--jwt-issuer', dest='issuer', default=None)
    interface.add_argument('--jwt-audience', dest='audience', default=None)
    tiers = interface.add_mutually_exclusive_group()
    tiers.add_argument('--read-only', dest='readonly', action='store_true')
    tiers.add_argument('--disable', default=None)
    return interface


class ServeApp(Application):
    """Serve HyperShell to MCP clients."""

    interface = serve_interface(PROGRAM, SERVE_USAGE, SERVE_HELP)
    ALLOW_NOARGS = True

    transport: str | None = None
    host: str | None = None
    port: int | None = None
    path: str | None = None
    site: str | None = None
    secretfile: str | None = None
    pubkey: str | None = None
    jwks: str | None = None
    anonymous: bool = False
    algorithm: str | None = None
    issuer: str | None = None
    audience: str | None = None
    readonly: bool = False
    disable: str | None = None

    def settings(self) -> Any:
        """Apply the site option, then merge these options over the configuration."""
        from hypershell_mcp.settings import load_settings
        choose_site(self.site)
        return load_settings(
            configured(), transport=self.transport, host=self.host, port=self.port, path=self.path,
            secretfile=self.secretfile, pubkey=self.pubkey, jwks=self.jwks, anonymous=self.anonymous,
            algorithm=self.algorithm, issuer=self.issuer, audience=self.audience,
            readonly=self.readonly or None, disable=self.disable)

    def run(self) -> None:
        from hypershell_mcp.server import build_server
        from hypershell_mcp.settings import LOOPBACK
        settings = self.settings()
        server = build_server(settings)
        if settings.transport == 'stdio':
            server.run('stdio', show_banner=False)
            return
        if settings.host not in LOOPBACK:
            print(f'{PROGRAM}: serving plain HTTP on {settings.host}: put it behind TLS (a reverse proxy or '
                  'a tunnel), or tokens cross the network in the clear', file=sys.stderr)
        if settings.anonymous:
            print(f'{PROGRAM}: serving WITHOUT authentication on {settings.host}:{settings.port}: anyone who '
                  'can reach this port can run commands as you', file=sys.stderr)
        # 'auto' checks the Host and Origin headers whenever the server is bound to loopback, which
        # is what stops a web page from reaching it through DNS rebinding.
        server.run('http', host=settings.host, port=settings.port, path=settings.path, show_banner=False,
                   host_origin_protection='auto', allowed_hosts=list(settings.hosts) or None)

    @classmethod
    def main(cls, argv: list[str] | None = None, **kwargs: Any) -> int:
        cls.exceptions = {**getattr(cls, 'exceptions', {}), **handled()}
        return super().main(argv, **kwargs)


# -------------------------------------------------------------------------------------------------

TOKEN_USAGE: Final[str] = f"""\
Usage:
  {PROGRAM} token [-h] [-s NAME] [--scopes TIERS] [--ttl DURATION] [--site DIR]
                  [--secret-file PATH | --private-key PATH] [--algorithm ALG]
                  [--issuer NAME] [--audience NAME]

  Sign a bearer token and print it.\
"""

TOKEN_HELP: Final[str] = f"""\
{TOKEN_USAGE}

  The token is signed with the secret the server verifies with (from --secret-file, or the
  mcp.jwt configuration), or with a private key whose public half the server holds.
  Give callers the least they need: a token is as powerful as its scopes for as long as
  it lives, and there is no revoking one short of changing the key.

Options:
  -s, --subject      NAME      Who the token is for; recorded in the audit log (default: you).
      --scopes       TIERS     Tiers to grant: read, submit, manage, run, admin, or all
                               (default: read).
      --ttl          DURATION  Lifetime, e.g. 30m, 8h, 7d (default: 8h).
      --secret-file  PATH      File holding the shared secret.
      --private-key  PATH      PEM private key (RS256 or ES256).
      --algorithm    ALG       Signature algorithm (default: by key type).
      --issuer       NAME      Issuer claim (default: as configured, else hypershell-mcp).
      --audience     NAME      Audience claim (default: as configured, else hypershell-mcp).
      --site         DIR       HyperShell site directory whose configuration to read.
  -h, --help                   Show this message and exit.\
"""


class TokenApp(Application):
    """Sign a bearer token."""

    interface = Interface(f'{PROGRAM} token', TOKEN_USAGE, TOKEN_HELP)
    ALLOW_NOARGS = True

    subject: str | None = None
    interface.add_argument('-s', '--subject', default=None)

    scopes: str = 'read'
    interface.add_argument('--scopes', default=scopes)

    ttl: str = '8h'
    interface.add_argument('--ttl', default=ttl)

    secretfile: str | None = None
    privatekey: str | None = None
    key_interface = interface.add_mutually_exclusive_group()
    key_interface.add_argument('--secret-file', dest='secretfile', default=None)
    key_interface.add_argument('--private-key', dest='privatekey', default=None)

    algorithm: str | None = None
    interface.add_argument('--algorithm', default=None)

    issuer: str | None = None
    interface.add_argument('--issuer', default=None)

    audience: str | None = None
    interface.add_argument('--audience', default=None)

    site: str | None = None
    interface.add_argument('--site', default=None)

    def run(self) -> None:
        from hypershell_mcp import auth
        from hypershell_mcp.settings import load_settings
        choose_site(self.site)
        scopes = auth.parse_scopes(self.scopes)
        ttl = auth.parse_ttl(self.ttl)
        if self.privatekey:
            key = auth.read_key_file(Path(self.privatekey))
            algorithm = self.algorithm or ('ES256' if 'EC PRIVATE' in key or self.is_ec(key) else 'RS256')
            issuer = self.issuer if self.issuer is not None else auth.DEFAULT_ISSUER
            audience = self.audience if self.audience is not None else auth.DEFAULT_AUDIENCE
        else:
            # Reuse the server's own resolution of the secret, issuer and audience.
            settings = load_settings(configured(), secretfile=self.secretfile, issuer=self.issuer,
                                     audience=self.audience, algorithm=self.algorithm)
            if not settings.auth.secret:
                raise auth.AuthError('no signing secret: pass --secret-file or --private-key, or set '
                                     f"mcp.jwt.secretfile (make one with '{PROGRAM} keygen')")
            settings.auth.validate()
            key, algorithm = settings.auth.secret, settings.auth.resolved_algorithm
            issuer, audience = settings.auth.issuer, settings.auth.audience
        print(auth.mint_token(key, algorithm=algorithm, subject=self.subject or getpass.getuser(),
                              scopes=scopes, ttl=ttl, issuer=issuer or None, audience=audience or None))

    @staticmethod
    def is_ec(pem: str) -> bool:
        """Whether a PKCS#8 ("BEGIN PRIVATE KEY") PEM holds an elliptic-curve key."""
        from joserfc.jwk import ECKey
        try:
            ECKey.import_key(pem)
            return True
        except Exception:
            return False

    @classmethod
    def main(cls, argv: list[str] | None = None, **kwargs: Any) -> int:
        cls.exceptions = {**getattr(cls, 'exceptions', {}), **handled()}
        return super().main(argv, **kwargs)


# -------------------------------------------------------------------------------------------------

KEYGEN_USAGE: Final[str] = f"""\
Usage:
  {PROGRAM} keygen [-h] [PATH] [--type TYPE]

  Make a signing secret or key pair.\
"""

KEYGEN_HELP: Final[str] = f"""\
{KEYGEN_USAGE}

  A secret both signs and verifies, so whoever runs the server can also make tokens, and
  so can any task the server runs as the same user. A key pair separates the two: the
  server holds only the public key and tokens are signed elsewhere with the private key.

Arguments:
  PATH                  Where to write the secret or private key (default: mcp.secret or
                        mcp.key beside the user's HyperShell configuration). The public
                        key of a pair is written to PATH.pub. Existing files are never
                        overwritten.

Options:
      --type    TYPE    secret (HS256), rsa (RS256) or ec (ES256) (default: secret).
  -h, --help            Show this message and exit.\
"""


class KeygenApp(Application):
    """Make a signing secret or key pair."""

    interface = Interface(f'{PROGRAM} keygen', KEYGEN_USAGE, KEYGEN_HELP)
    ALLOW_NOARGS = True

    path: str | None = None
    interface.add_argument('path', nargs='?', default=None)

    kind: str = 'secret'
    interface.add_argument('--type', dest='kind', default=kind, choices=['secret', 'rsa', 'ec'])

    def run(self) -> None:
        from hypershell_mcp import auth
        target = Path(self.path).expanduser() if self.path else self.default_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == 'secret':
            auth.write_private(target, auth.generate_secret())
            print(f'Wrote a signing secret to {target} (readable only by you).\n\n'
                  f'Serve with it:\n  {PROGRAM} --transport http --jwt-secret-file {target}\n'
                  f'or set it once:\n  hs config set mcp.jwt.secretfile {target}\n\n'
                  f'Sign a token:\n  {PROGRAM} token --secret-file {target} --scopes read,submit --ttl 8h')
            return
        private, public, algorithm = auth.generate_keypair(self.kind)
        public_path = target.with_name(target.name + '.pub')
        if public_path.exists():
            raise auth.AuthError(f'{public_path} already exists; refusing to overwrite a key')
        auth.write_private(target, private)
        public_path.write_text(public)
        print(f'Wrote a {algorithm} private key to {target} (readable only by you)\n'
              f'and its public key to {public_path}.\n\n'
              f'Serve with the public key only:\n  {PROGRAM} --transport http --jwt-public-key {public_path}\n\n'
              f'Sign tokens wherever the private key is kept:\n'
              f'  {PROGRAM} token --private-key {target} --scopes read,submit --ttl 8h')

    def default_path(self) -> Path:
        from hypershell.core.platform import path as site_path
        folder = Path(str(site_path.user.config)).parent
        return folder / ('mcp.secret' if self.kind == 'secret' else 'mcp.key')

    @classmethod
    def main(cls, argv: list[str] | None = None, **kwargs: Any) -> int:
        cls.exceptions = {**getattr(cls, 'exceptions', {}), **handled()}
        return super().main(argv, **kwargs)


# -------------------------------------------------------------------------------------------------

CHECK_USAGE: Final[str] = f"""\
Usage:
  {PROGRAM} check [-h] [serve options...]

  Show what would be served, without serving.\
"""

CHECK_HELP: Final[str] = f"""\
{CHECK_USAGE}

  Takes the same options as serve. Prints the HyperShell site and database the server would
  act on, how callers would be authenticated, and the tools in each tier. Exits non-zero
  when the setup would be refused.\
"""


class CheckApp(ServeApp):
    """Show the effective setup."""

    interface = serve_interface(f'{PROGRAM} check', CHECK_USAGE, CHECK_HELP)

    def run(self) -> None:
        import asyncio

        from hypershell_mcp.auth import TIERS
        from hypershell_mcp.backend import hostname
        from hypershell_mcp.server import build_server, describe
        settings = self.settings()
        server = build_server(settings)
        app = server.state  # type: ignore[attr-defined]
        summary = describe(settings)
        database = app.backend.database_info()
        counts = asyncio.run(app.backend.call(app.backend.count, None))
        print(f"HyperShell {summary['hypershell']}, hypershell-mcp {summary['server']}, on {hostname()}")
        print(f'Database:   {database.provider} at {database.location} ({counts.total} tasks)')
        print(f'State:      {app.state_dir}')
        endpoint = ('stdio' if settings.transport == 'stdio'
                    else f'http://{settings.host}:{settings.port}{settings.path}')
        print(f'Transport:  {endpoint}')
        print(f"Auth:       {summary['authentication']}"
              + (f" (issuer {summary['issuer'] or 'any'}, audience {summary['audience'] or 'any'})"
                 if settings.auth.enabled else ''))
        print('Tools:')
        for tier in TIERS:
            names = sorted(name for name, owner in app.tool_tiers.items() if owner is tier)
            state = '' if tier in settings.tiers else '  (disabled)'
            print(f"  {tier.value:<7} {', '.join(names)}{state}")


# -------------------------------------------------------------------------------------------------

COMMANDS: Final[dict[str, type[Application]]] = {
    'serve': ServeApp, 'token': TokenApp, 'keygen': KeygenApp, 'check': CheckApp}


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch to a command, serving when none is named."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]].main(argv[1:])
    return ServeApp.main(argv)
