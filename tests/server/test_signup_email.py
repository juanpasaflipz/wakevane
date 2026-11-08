"""Open-signup email rules and the SMTP mailer's configuration."""

from __future__ import annotations

import pytest

from omnigent.server.mailer import SmtpMailer
from omnigent.server.signup_email import blocked_domain, normalize_email


@pytest.mark.parametrize(
    ("raw", "email", "key"),
    [
        (" Carol.Smith+wv@Gmail.com ", "carol.smith+wv@gmail.com", "carolsmith@gmail.com"),
        ("erin.k@googlemail.com", "erin.k@googlemail.com", "erink@gmail.com"),
        ("dev+trial@example.org", "dev+trial@example.org", "dev@example.org"),
        ("plain@example.org", "plain@example.org", "plain@example.org"),
    ],
)
def test_normalize_email_collapses_provider_aliases(raw: str, email: str, key: str) -> None:
    assert normalize_email(raw) == (email, key, None)


def test_normalize_email_handles_empty_and_malformed_input() -> None:
    assert normalize_email(None) == (None, None, None)
    assert normalize_email("   ") == (None, None, None)
    for bad in ("nope", "a@b", "two@@x.io", "x" * 250 + "@a.io"):
        _, _, error = normalize_email(bad)
        assert error is not None, bad


def test_blocked_domain_uses_builtin_and_operator_lists() -> None:
    assert blocked_domain("x@mailinator.com")
    assert not blocked_domain("x@example.org")
    assert blocked_domain(
        "x@corp.test", {"OMNIGENT_SIGNUP_BLOCKED_EMAIL_DOMAINS": "Corp.test, other.io"}
    )


def test_mailer_from_environment() -> None:
    assert SmtpMailer.from_environment({}) is None
    mailer = SmtpMailer.from_environment(
        {
            "OMNIGENT_SMTP_HOST": "smtp.gmail.com",
            "OMNIGENT_SMTP_USERNAME": "ops@example.org",
            "OMNIGENT_SMTP_PASSWORD": "app-password-synthetic",
            "OMNIGENT_SMTP_FROM": "WakeVane <hello@example.org>",
        }
    )
    assert mailer == SmtpMailer(
        host="smtp.gmail.com",
        port=587,
        username="ops@example.org",
        password="app-password-synthetic",
        sender="WakeVane <hello@example.org>",
    )
    with pytest.raises(ValueError):
        SmtpMailer.from_environment({"OMNIGENT_SMTP_HOST": "smtp.gmail.com"})
