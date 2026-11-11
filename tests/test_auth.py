# SPDX-FileCopyrightText: 2026 Geoffrey Lentner
# SPDX-License-Identifier: Apache-2.0

"""Tokens, keys and the settings that decide whether a server may be started."""

import stat
import time

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from hypershell_mcp import auth
from hypershell_mcp.auth import AuthError, AuthSettings, Tier
from hypershell_mcp.settings import SettingsError, load_settings

pytestmark = pytest.mark.unit


async def verify(settings: AuthSettings, token: str):
    return await auth.build_verifier(settings).load_access_token(token)


def raw_token(secret: str, **claims) -> str:
    base = {'sub': 'x', 'iss': auth.DEFAULT_ISSUER, 'aud': auth.DEFAULT_AUDIENCE, 'scope': 'hypershell:read'}
    return jwt.encode({'alg': 'HS256'}, {**base, **claims}, OctKey.import_key(secret))


async def test_minted_token_verifies_with_its_scopes(secret):
    token = auth.mint_token(secret, subject='ada', scopes=auth.parse_scopes('read,run'), ttl=60)
    access = await verify(AuthSettings(secret=secret), token)
    assert access.subject == 'ada'
    assert access.scopes == ['hypershell:read', 'hypershell:run']
    assert access.claims['jti']


@pytest.mark.parametrize('kind', ['rsa', 'ec'])
async def test_key_pairs_sign_and_verify(kind):
    private, public, algorithm = auth.generate_keypair(kind)
    token = auth.mint_token(private, algorithm=algorithm, subject='ada', scopes=['hypershell:read'], ttl=60)
    assert await verify(AuthSettings(public_key=public, algorithm=algorithm), token) is not None
    other_public = auth.generate_keypair(kind)[1]
    assert await verify(AuthSettings(public_key=other_public, algorithm=algorithm), token) is None


async def test_token_without_expiry_is_refused(secret):
    assert await verify(AuthSettings(secret=secret), raw_token(secret)) is None


async def test_token_not_yet_valid_is_refused(secret):
    now = int(time.time())
    assert await verify(AuthSettings(secret=secret), raw_token(secret, exp=now + 900, nbf=now + 600)) is None
    assert await verify(AuthSettings(secret=secret), raw_token(secret, exp=now + 900, nbf=now - 5)) is not None


async def test_expired_wrong_audience_and_wrong_secret_are_refused(secret):
    settings = AuthSettings(secret=secret)
    assert await verify(settings, raw_token(secret, exp=int(time.time()) - 5)) is None
    assert await verify(settings, auth.mint_token(secret, subject='x', scopes=[], ttl=60, audience='other')) is None
    assert await verify(settings, auth.mint_token(secret, subject='x', scopes=[], ttl=60, issuer='other')) is None
    forged = auth.mint_token(auth.generate_secret(), subject='x', scopes=['hypershell:admin'], ttl=60)
    assert await verify(settings, forged) is None


def test_scopes_and_durations_parse():
    assert auth.parse_scopes('read, submit') == ['hypershell:read', 'hypershell:submit']
    assert auth.parse_scopes('all') == [auth.scope_for(tier) for tier in auth.TIERS]
    assert auth.parse_scopes('read other:thing read') == ['hypershell:read', 'other:thing']
    with pytest.raises(AuthError, match='unknown scope'):
        auth.parse_scopes('root')
    assert [auth.parse_ttl(text) for text in ('90', '15m', '8h', '2d')] == [90, 900, 28800, 172800]
    with pytest.raises(AuthError):
        auth.parse_ttl('soon')


def test_weak_or_mismatched_keys_are_refused(secret):
    with pytest.raises(AuthError, match='at least 32'):
        AuthSettings(secret='hunter2').validate()
    with pytest.raises(AuthError, match='needs a public key'):
        AuthSettings(secret=secret, algorithm='RS256').validate()
    with pytest.raises(AuthError, match='needs a shared secret'):
        AuthSettings(public_key='-----BEGIN PUBLIC KEY-----', algorithm='HS256').validate()
    with pytest.raises(AuthError, match='one key source'):
        AuthSettings(secret=secret, jwks_uri='https://idp.example/jwks').validate()


def test_private_key_files_are_owner_only_and_never_overwritten(tmp_path):
    target = tmp_path / 'secret'
    auth.write_private(target, auth.generate_secret())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(AuthError, match='already exists'):
        auth.write_private(target, 'again')


def test_http_needs_a_key_unless_waived_on_loopback(secret):
    with pytest.raises(SettingsError, match='HTTP needs a JWT key'):
        load_settings({}, transport='http')
    assert load_settings({}, transport='http', secret=secret).auth.enabled
    assert load_settings({}, transport='http', anonymous=True).anonymous
    with pytest.raises(SettingsError, match='refusing to serve 0.0.0.0 without authentication'):
        load_settings({}, transport='http', anonymous=True, host='0.0.0.0')
    with pytest.raises(SettingsError, match='choose one'):
        load_settings({}, transport='http', anonymous=True, secret=secret)


def test_settings_come_from_the_mcp_section_and_options_override(secret, tmp_path):
    keyfile = tmp_path / 'key'
    keyfile.write_text(secret + '\n')
    section = {'transport': 'http', 'port': 9000, 'disable': ['admin', 'run'],
               'jwt': {'secretfile': str(keyfile), 'issuer': 'lab'}, 'limit': {'rows': 25}}
    settings = load_settings(section)
    assert (settings.transport, settings.port, settings.auth.secret, settings.auth.issuer) == ('http', 9000, secret, 'lab')
    assert settings.tiers == {Tier.READ, Tier.SUBMIT, Tier.MANAGE}
    assert settings.limits.rows == 25 and settings.limits.wait == 600
    assert load_settings(section, port=9100, readonly=True).tiers == {Tier.READ}
    assert load_settings(section, port=9100).port == 9100
    with pytest.raises(SettingsError, match='read tier cannot be disabled'):
        load_settings({'disable': 'read'})
    with pytest.raises(SettingsError, match='tiers are'):
        load_settings({'disable': 'everything'})
