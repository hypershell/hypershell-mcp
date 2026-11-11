# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""
Authentication and authorization.

Over HTTP every request carries a JWT bearer token. The token is verified with a key: a shared
secret (HS256), a PEM public key (RS256, ES256, Ed25519), or a JWKS endpoint. What the caller may do
is decided by the scopes in the token, one scope per *tier* of tools.

Over stdio there is no token. The process was started by the local user, who could run ``hs``
directly, so every tier is open unless the server was started read-only.
"""

from __future__ import annotations

import os
import secrets
import stat
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from fastmcp.server.auth import AccessToken, JWTVerifier, restrict_tag
from fastmcp.server.middleware import AuthMiddleware
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey, OKPKey, RSAKey

__all__ = ['Tier', 'TIERS', 'scope_for', 'AuthSettings', 'AuthError', 'StrictJWTVerifier',
           'build_verifier', 'build_authorization', 'mint_token', 'generate_secret',
           'generate_keypair', 'write_private', 'parse_scopes', 'parse_ttl',
           'DEFAULT_ISSUER', 'DEFAULT_AUDIENCE', 'MIN_SECRET_BYTES']


class Tier(StrEnum):
    """
    A tier groups tools by what they can do. Each tool carries exactly one tier as a tag, and a
    token needs the matching scope to see or call it.
    """

    READ = 'read'
    """Look at tasks, output, configuration and status. Changes nothing."""

    SUBMIT = 'submit'
    """Add tasks to the queue. A submitted task is a shell command a client will run."""

    MANAGE = 'manage'
    """Cancel, retry, re-tag and delete tasks that already exist."""

    RUN = 'run'
    """Start and stop clusters on the server's host: this is what executes commands."""

    ADMIN = 'admin'
    """Initialise, vacuum, rotate and back up the database; change configuration."""


TIERS: Final[tuple[Tier, ...]] = tuple(Tier)
SCOPE_PREFIX: Final[str] = 'hypershell:'
DEFAULT_ISSUER: Final[str] = 'hypershell-mcp'
DEFAULT_AUDIENCE: Final[str] = 'hypershell-mcp'
MIN_SECRET_BYTES: Final[int] = 32
"""An HS256 secret shorter than its 256-bit digest can be brute-forced offline from one token."""

SYMMETRIC: Final[frozenset[str]] = frozenset({'HS256', 'HS384', 'HS512'})
ASYMMETRIC: Final[frozenset[str]] = frozenset({'RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512',
                                              'ES256', 'ES384', 'ES512', 'EdDSA', 'Ed25519', 'Ed448'})
CLOCK_SKEW: Final[int] = 30
"""Seconds of disagreement tolerated between the issuer's clock and ours for ``nbf``."""


class AuthError(Exception):
    """The authentication settings are unusable; the message says what to fix."""


def scope_for(tier: Tier | str) -> str:
    """The OAuth scope that grants a tier, e.g. ``hypershell:read``."""
    return f'{SCOPE_PREFIX}{Tier(tier).value}'


def parse_scopes(text: str) -> list[str]:
    """
    Turn ``read,submit`` or ``all`` or ``hypershell:read hypershell:run`` into a list of scopes.

    Tier names are expanded to their scopes. Anything else that looks like a scope (it has a
    colon) is kept as written, so tokens can carry scopes for other services too.
    """
    scopes: list[str] = []
    for word in text.replace(',', ' ').split():
        if word == 'all':
            found = [scope_for(tier) for tier in TIERS]
        elif word in Tier._value2member_map_:
            found = [scope_for(word)]
        elif ':' in word:
            found = [word]
        else:
            names = ', '.join(tier.value for tier in TIERS)
            raise AuthError(f'unknown scope {word!r}: use one of {names}, or all')
        scopes.extend(scope for scope in found if scope not in scopes)
    return scopes


def parse_ttl(text: str) -> int:
    """Seconds from a duration such as ``90``, ``15m``, ``8h`` or ``30d``."""
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    text = text.strip().lower()
    scale = units.get(text[-1:], None)
    number = text[:-1] if scale else text
    try:
        seconds = int(float(number) * (scale or 1))
    except ValueError:
        raise AuthError(f'cannot read {text!r} as a duration: use e.g. 900, 15m, 8h or 30d') from None
    if seconds <= 0:
        raise AuthError('a token lifetime must be positive')
    return seconds


@dataclass(frozen=True)
class AuthSettings:
    """How bearer tokens are verified. Exactly one of the three key sources is set."""

    secret: str | None = None
    """Shared secret for HS256/384/512."""

    public_key: str | None = None
    """PEM public key for RS*, PS*, ES* or Ed25519."""

    jwks_uri: str | None = None
    """URL of a JWKS document published by an identity provider."""

    algorithm: str | None = None
    """Signature algorithm. Defaults to HS256 with a secret and RS256 otherwise."""

    issuer: str | None = DEFAULT_ISSUER
    """Required ``iss`` claim, or None to accept any issuer."""

    audience: str | None = DEFAULT_AUDIENCE
    """Required ``aud`` claim, or None to accept any audience."""

    @property
    def enabled(self) -> bool:
        return bool(self.secret or self.public_key or self.jwks_uri)

    @property
    def resolved_algorithm(self) -> str:
        return self.algorithm or ('HS256' if self.secret else 'RS256')

    def validate(self) -> None:
        """Raise `AuthError` unless the settings describe one usable key source."""
        sources = [name for name, value in (('a secret', self.secret), ('a public key', self.public_key),
                                            ('a JWKS URI', self.jwks_uri)) if value]
        if len(sources) > 1:
            raise AuthError(f'give one key source, not {" and ".join(sources)}')
        if not sources:
            raise AuthError('no key: give a JWT secret, a public key or a JWKS URI')
        algorithm = self.resolved_algorithm
        if self.secret:
            if algorithm not in SYMMETRIC:
                raise AuthError(f'{algorithm} needs a public key; a secret works with HS256, HS384 or HS512')
            if len(self.secret.encode()) < MIN_SECRET_BYTES:
                raise AuthError(f'the JWT secret is {len(self.secret.encode())} bytes; use at least '
                                f"{MIN_SECRET_BYTES} (make one with 'hypershell-mcp keygen')")
        elif algorithm not in ASYMMETRIC:
            raise AuthError(f'{algorithm} needs a shared secret; a public key or JWKS works with '
                            'RS256, PS256, ES256, Ed25519 and their larger variants')


class StrictJWTVerifier(JWTVerifier):
    """
    A JWT verifier that also insists on an expiry and honours ``nbf``.

    FastMCP's verifier checks the signature, issuer, audience and ``exp`` when present. A token
    without ``exp`` would then be valid forever, and one with a future ``nbf`` valid early. Neither
    belongs on a server that can run shell commands, so both are refused here.
    """

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        if not isinstance(claims.get('exp'), (int, float)):
            self.logger.warning('Bearer token rejected for client %s: no expiry (exp)', access.client_id)
            return None
        not_before = claims.get('nbf')
        if isinstance(not_before, (int, float)) and not_before > time.time() + CLOCK_SKEW:
            self.logger.warning('Bearer token rejected for client %s: not valid yet (nbf)', access.client_id)
            return None
        return access


def build_verifier(settings: AuthSettings) -> StrictJWTVerifier:
    """The token verifier for these settings (see `AuthSettings.validate` for what is refused)."""
    settings.validate()
    return StrictJWTVerifier(
        public_key=settings.secret or settings.public_key,
        jwks_uri=settings.jwks_uri,
        algorithm=settings.resolved_algorithm,
        issuer=settings.issuer,
        audience=settings.audience,
    )


def build_authorization() -> AuthMiddleware:
    """
    Middleware that maps tiers to scopes: a component tagged ``submit`` needs ``hypershell:submit``.

    A component the token cannot use is left out of listings, and calling it anyway is answered
    with the missing scope, not a bare "unknown tool". FastMCP skips these checks over stdio.
    """
    return AuthMiddleware(auth=[restrict_tag(tier.value, scopes=[scope_for(tier)]) for tier in TIERS])


def signing_key(key: str, algorithm: str) -> OctKey | RSAKey | ECKey | OKPKey:
    """Import a secret or PEM private key for signing with `algorithm`."""
    if algorithm in SYMMETRIC:
        return OctKey.import_key(key)
    if algorithm.startswith(('RS', 'PS')):
        return RSAKey.import_key(key)
    if algorithm.startswith('ES'):
        return ECKey.import_key(key)
    if algorithm in ('EdDSA', 'Ed25519', 'Ed448'):
        return OKPKey.import_key(key)
    raise AuthError(f'unsupported algorithm {algorithm}')


def mint_token(key: str, *, algorithm: str = 'HS256', subject: str, scopes: list[str], ttl: int,
               issuer: str | None = DEFAULT_ISSUER, audience: str | None = DEFAULT_AUDIENCE,
               claims: dict[str, Any] | None = None) -> str:
    """
    Sign a bearer token.

    `key` is the shared secret for HS* or a PEM private key otherwise. `ttl` is the lifetime in
    seconds. The token gets a random ``jti`` so that one token can be told from another in logs.
    """
    now = int(time.time())
    payload: dict[str, Any] = {**(claims or {}), 'sub': subject, 'scope': ' '.join(scopes),
                               'iat': now, 'nbf': now, 'exp': now + ttl, 'jti': uuid.uuid4().hex}
    if issuer:
        payload['iss'] = issuer
    if audience:
        payload['aud'] = audience
    return jwt.encode({'alg': algorithm, 'typ': 'JWT'}, payload, signing_key(key, algorithm),
                      algorithms=[algorithm])


def generate_secret() -> str:
    """A new random shared secret (384 bits, URL-safe text)."""
    return secrets.token_urlsafe(48)


def generate_keypair(kind: str) -> tuple[str, str, str]:
    """
    A new key pair as ``(private PEM, public PEM, algorithm)``.

    `kind` is ``rsa`` (RS256, 3072-bit) or ``ec`` (ES256, P-256).
    """
    if kind == 'rsa':
        key, algorithm = RSAKey.generate_key(3072), 'RS256'
    elif kind == 'ec':
        key, algorithm = ECKey.generate_key('P-256'), 'ES256'
    else:
        raise AuthError(f'unknown key type {kind!r}: use secret, rsa or ec')
    return key.as_pem(private=True).decode(), key.as_pem(private=False).decode(), algorithm


def write_private(path: Path, text: str) -> None:
    """Write key material to a new file that only its owner can read. Never overwrites."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        raise AuthError(f'{path} already exists; refusing to overwrite a key') from None
    with os.fdopen(fd, 'w') as stream:
        stream.write(text if text.endswith('\n') else text + '\n')


def read_key_file(path: Path) -> str:
    """Read a secret or PEM key from a file, warning-free about trailing newlines."""
    try:
        text = path.expanduser().read_text()
    except OSError as error:
        raise AuthError(f'cannot read key file {path}: {error.strerror}') from None
    return text.strip() if '-----BEGIN' not in text else text
