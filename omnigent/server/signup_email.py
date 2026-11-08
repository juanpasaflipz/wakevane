"""Contact-email rules for open signup: shape, canonical key and blocked domains.

The canonical key lets one mailbox be recognised across accounts: Gmail
ignores dots and anything after ``+`` in the local part, and most providers
honour ``+`` sub-addressing, so those are collapsed before comparing.
"""

from __future__ import annotations

import os
import re

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
_DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
# Throwaway providers that defeat "one mailbox, one credit". Operators extend
# the list with OMNIGENT_SIGNUP_BLOCKED_EMAIL_DOMAINS (comma-separated).
_DISPOSABLE_DOMAINS = frozenset(
    {
        "10minutemail.com",
        "dispostable.com",
        "getnada.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "mailinator.com",
        "maildrop.cc",
        "mohmal.com",
        "sharklasers.com",
        "temp-mail.org",
        "tempmail.com",
        "throwawaymail.com",
        "trashmail.com",
        "yopmail.com",
    }
)
BLOCKED_DOMAINS_ENV = "OMNIGENT_SIGNUP_BLOCKED_EMAIL_DOMAINS"


def normalize_email(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Return ``(email, key, error)`` for a submitted address.

    ``email`` is the lowercased address to store and send to; ``key`` is the
    canonical form used to detect the same mailbox behind aliases. An empty
    submission yields ``(None, None, None)``; a malformed one sets ``error``.
    """
    if raw is None:
        return None, None, None
    email = raw.strip().lower()
    if email == "":
        return None, None, None
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        return None, None, "email must look like name@example.com"
    local, _, domain = email.rpartition("@")
    if domain in _DOT_INSENSITIVE_DOMAINS:
        domain = "gmail.com"
        local = local.replace(".", "")
    local = local.split("+", 1)[0]
    return email, f"{local}@{domain}", None


def blocked_domain(key: str, environ: dict[str, str] | None = None) -> bool:
    """True when the canonical address belongs to a throwaway provider."""
    env = environ if environ is not None else os.environ
    extra = {d.strip().lower() for d in env.get(BLOCKED_DOMAINS_ENV, "").split(",") if d.strip()}
    domain = key.rpartition("@")[2]
    return domain in _DISPOSABLE_DOMAINS or domain in extra
