"""Signed, single-use password reset tokens for the ``accounts`` provider.

A token names the account and the account generation it was issued under.
Resetting a password rotates the generation, so a redeemed token (and every
other token issued before the reset) stops verifying without any stored
state.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import jwt

_ALGORITHM = "HS256"
_PURPOSE = "password_reset"
RESET_TOKEN_TTL_SECONDS = 60 * 60


def _signing_key(cookie_secret: bytes) -> bytes:
    """A key separate from the session-cookie key, derived from the same secret."""
    return hmac.new(cookie_secret, b"omnigent/password-reset/v1", hashlib.sha256).digest()


def mint_reset_token(
    *,
    user_id: str,
    account_generation: str,
    cookie_secret: bytes,
    now_epoch_seconds: int | None = None,
    ttl_seconds: int = RESET_TOKEN_TTL_SECONDS,
) -> str:
    """Issue a reset token for ``user_id`` valid for ``ttl_seconds``.

    :param account_generation: The account's current generation, e.g.
        ``"9f1c…"``; the token dies when it rotates.
    :returns: A compact signed token safe to place in a URL query.
    """
    now = now_epoch_seconds if now_epoch_seconds is not None else int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "gen": account_generation,
            "purpose": _PURPOSE,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        _signing_key(cookie_secret),
        algorithm=_ALGORITHM,
    )


def read_reset_token(token: str, *, cookie_secret: bytes) -> tuple[str, str] | None:
    """Verify a reset token's signature, purpose and expiry.

    :returns: ``(user_id, account_generation)``, or ``None`` for a forged,
        expired or malformed token. The caller still has to confirm the
        generation matches the account's current one.
    """
    try:
        claims = jwt.decode(
            token,
            _signing_key(cookie_secret),
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None
    user_id = claims.get("sub")
    generation = claims.get("gen")
    if claims.get("purpose") != _PURPOSE:
        return None
    if not isinstance(user_id, str) or not isinstance(generation, str):
        return None
    if not user_id or not generation:
        return None
    return user_id, generation
