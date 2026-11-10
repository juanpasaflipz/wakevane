"""Attempt limits for the unauthenticated ``/auth`` routes.

A public deployment with open signup exposes password verification, account
creation and reset mail to anyone. Each of those costs an argon2 hash or an
outbound message, so they are bounded per client address and per account.

Counters live in this process. Behind several replicas each one enforces its
own budget, so the effective ceiling is the configured limit times the
replica count; pair it with an edge rule when that matters.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from starlette.requests import Request

_DISABLE_ENV = "OMNIGENT_ACCOUNTS_RATE_LIMIT"
# Stale keys are swept once the table grows past this many entries.
_SWEEP_THRESHOLD = 10_000


@dataclass(frozen=True)
class RateLimit:
    """At most ``max_attempts`` recorded attempts per ``window_seconds``."""

    max_attempts: int
    window_seconds: int


# A wrong password from one address against one account.
LOGIN_FAILURES_PER_ADDRESS_AND_ACCOUNT = RateLimit(max_attempts=5, window_seconds=15 * 60)
# Wrong passwords from one address across all accounts (credential stuffing).
LOGIN_FAILURES_PER_ADDRESS = RateLimit(max_attempts=30, window_seconds=15 * 60)
# Wrong passwords against one account from any address (rotating proxies).
LOGIN_FAILURES_PER_ACCOUNT = RateLimit(max_attempts=20, window_seconds=15 * 60)
REGISTRATIONS_PER_ADDRESS = RateLimit(max_attempts=10, window_seconds=60 * 60)
RESET_REQUESTS_PER_ADDRESS = RateLimit(max_attempts=5, window_seconds=60 * 60)
RESET_REQUESTS_PER_ACCOUNT = RateLimit(max_attempts=3, window_seconds=60 * 60)
RESET_REDEMPTIONS_PER_ADDRESS = RateLimit(max_attempts=10, window_seconds=60 * 60)


def rate_limiting_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether attempt limits apply; ``OMNIGENT_ACCOUNTS_RATE_LIMIT=0`` turns them off."""
    env = environ if environ is not None else os.environ
    return env.get(_DISABLE_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def client_address(request: Request) -> str:
    """The caller's network address, preferring what the edge proxy forwards.

    :returns: An address such as ``"203.0.113.7"``, or ``""`` when unknown.
    """
    return (
        request.headers.get("cf-connecting-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )


class AttemptLimiter:
    """Sliding-window attempt counter keyed by caller-chosen strings.

    :param enabled: When false every check passes and nothing is recorded.
    :param clock: Seconds source, injectable for tests.
    """

    def __init__(
        self, *, enabled: bool = True, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._enabled = enabled
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}
        self._longest_window = 0

    def retry_after(self, key: str, limit: RateLimit) -> int:
        """Seconds until ``key`` may try again; ``0`` when it is under the limit."""
        if not self._enabled:
            return 0
        now = self._clock()
        with self._lock:
            attempts = self._pruned(key, limit, now)
            if attempts is None or len(attempts) < limit.max_attempts:
                return 0
            oldest_counted = attempts[-limit.max_attempts]
            return max(1, int(oldest_counted + limit.window_seconds - now) + 1)

    def record(self, key: str, limit: RateLimit) -> None:
        """Count one attempt against ``key``."""
        if not self._enabled:
            return
        now = self._clock()
        with self._lock:
            self._longest_window = max(self._longest_window, limit.window_seconds)
            self._pruned(key, limit, now)
            self._attempts.setdefault(key, deque()).append(now)
            if len(self._attempts) > _SWEEP_THRESHOLD:
                self._sweep(now)

    def reset(self, key: str) -> None:
        """Forget ``key``, e.g. after a successful sign-in."""
        with self._lock:
            self._attempts.pop(key, None)

    def _pruned(self, key: str, limit: RateLimit, now: float) -> deque[float] | None:
        attempts = self._attempts.get(key)
        if attempts is None:
            return None
        cutoff = now - limit.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            del self._attempts[key]
            return None
        return attempts

    def _sweep(self, now: float) -> None:
        cutoff = now - self._longest_window
        stale = [key for key, attempts in self._attempts.items() if attempts[-1] <= cutoff]
        for key in stale:
            del self._attempts[key]
