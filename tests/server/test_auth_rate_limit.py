"""Unit tests for the auth attempt limiter, reset tokens and security headers."""

from __future__ import annotations

import pytest

from omnigent.server.auth_rate_limit import AttemptLimiter, RateLimit, rate_limiting_enabled
from omnigent.server.password_reset import mint_reset_token, read_reset_token
from omnigent.server.security_headers import build_security_headers

_SECRET = b"s" * 32


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_blocks_at_the_limit_and_reopens_as_the_window_slides() -> None:
    clock = _Clock()
    limiter = AttemptLimiter(clock=clock)
    limit = RateLimit(max_attempts=2, window_seconds=60)

    assert limiter.retry_after("k", limit) == 0
    limiter.record("k", limit)
    clock.now += 10
    limiter.record("k", limit)
    assert limiter.retry_after("other", limit) == 0
    # The first attempt leaves the window 50 s from now.
    assert 50 <= limiter.retry_after("k", limit) <= 51

    clock.now += 51
    assert limiter.retry_after("k", limit) == 0
    limiter.record("k", limit)
    assert limiter.retry_after("k", limit) > 0


def test_limiter_reset_and_disabled_mode() -> None:
    limit = RateLimit(max_attempts=1, window_seconds=60)
    limiter = AttemptLimiter(clock=_Clock())
    limiter.record("k", limit)
    assert limiter.retry_after("k", limit) > 0
    limiter.reset("k")
    assert limiter.retry_after("k", limit) == 0

    disabled = AttemptLimiter(enabled=False)
    disabled.record("k", limit)
    disabled.record("k", limit)
    assert disabled.retry_after("k", limit) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", True), ("1", True), ("0", False), ("off", False), ("FALSE", False)],
)
def test_rate_limiting_enabled_reads_the_switch(value: str, expected: bool) -> None:
    assert rate_limiting_enabled({"OMNIGENT_ACCOUNTS_RATE_LIMIT": value}) is expected


def test_reset_token_round_trips_and_rejects_tampering() -> None:
    token = mint_reset_token(
        user_id="ana", account_generation="gen-1", cookie_secret=_SECRET, now_epoch_seconds=None
    )
    assert read_reset_token(token, cookie_secret=_SECRET) == ("ana", "gen-1")
    assert read_reset_token(token, cookie_secret=b"t" * 32) is None
    assert read_reset_token(token + "x", cookie_secret=_SECRET) is None
    assert read_reset_token("", cookie_secret=_SECRET) is None


def test_reset_token_expires() -> None:
    expired = mint_reset_token(
        user_id="ana",
        account_generation="gen-1",
        cookie_secret=_SECRET,
        now_epoch_seconds=1_000,
        ttl_seconds=60,
    )
    assert read_reset_token(expired, cookie_secret=_SECRET) is None


def test_security_headers_follow_scheme_and_frame_setting() -> None:
    assert build_security_headers(https=False, environ={}) == {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    secure = build_security_headers(https=True, environ={"OMNIGENT_FRAME_ANCESTORS": "'none'"})
    assert secure["Strict-Transport-Security"] == "max-age=31536000"
    assert secure["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert secure["X-Frame-Options"] == "DENY"

    listed = build_security_headers(
        https=True, environ={"OMNIGENT_FRAME_ANCESTORS": "'self' https://ide.example.com"}
    )
    assert listed["Content-Security-Policy"] == "frame-ancestors 'self' https://ide.example.com"
    assert "X-Frame-Options" not in listed


def test_security_headers_reject_directive_injection() -> None:
    with pytest.raises(ValueError, match="OMNIGENT_FRAME_ANCESTORS"):
        build_security_headers(
            https=True, environ={"OMNIGENT_FRAME_ANCESTORS": "'self'; script-src *"}
        )
