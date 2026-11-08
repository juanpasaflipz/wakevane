"""Outbound email over SMTP for account messages (verification links).

Configured entirely from the environment; absent settings mean the server
sends nothing and callers degrade (an account is created without the
message, and the operator sees a warning in the log).

Settings: ``OMNIGENT_SMTP_HOST``, ``OMNIGENT_SMTP_PORT`` (default 587, STARTTLS),
``OMNIGENT_SMTP_USERNAME``, ``OMNIGENT_SMTP_PASSWORD``, ``OMNIGENT_SMTP_FROM``
(for example ``WakeVane <hello@example.com>``; defaults to the username).
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpMailer:
    """One SMTP relay; ``send`` opens a fresh connection per message."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    timeout_s: float = 20.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> SmtpMailer | None:
        env = environ if environ is not None else os.environ
        host = env.get("OMNIGENT_SMTP_HOST", "").strip()
        if not host:
            return None
        username = env.get("OMNIGENT_SMTP_USERNAME", "").strip()
        password = env.get("OMNIGENT_SMTP_PASSWORD", "")
        if not username or not password:
            raise ValueError(
                "OMNIGENT_SMTP_HOST needs OMNIGENT_SMTP_USERNAME and OMNIGENT_SMTP_PASSWORD"
            )
        return cls(
            host=host,
            port=int(env.get("OMNIGENT_SMTP_PORT", "587")),
            username=username,
            password=password,
            sender=env.get("OMNIGENT_SMTP_FROM", "").strip() or username,
        )

    def send(self, *, to: str, subject: str, text: str) -> None:
        """Deliver one plain-text message; raises on relay errors."""
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as relay:
            relay.ehlo()
            relay.starttls()
            relay.login(self.username, self.password)
            relay.send_message(message)
        _logger.info("mail sent: %s to %s", subject, to.rsplit("@", 1)[-1])
