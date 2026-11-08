"""Approval emails for unattended (subscription-launched) sessions.

Real SQLite conversation and permission stores; the mailer and account lookup
are recording stand-ins so the test can assert on exactly what would be sent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server import approval_mailer
from omnigent.server.approval_mailer import ApprovalEmailNotifier, fan_out_observers
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

OWNER = "root"
LEVEL_OWNER = 4


class RecordingMailer:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text})


class Accounts:
    def __init__(self, **accounts: SimpleNamespace) -> None:
        self._accounts = accounts

    def get_user(self, user_id: str) -> SimpleNamespace | None:
        return self._accounts.get(user_id)


VERIFIED = SimpleNamespace(email="owner@example.com", email_verified_at=1794150000)


@pytest.fixture(autouse=True)
def no_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _skip(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(approval_mailer, "_grace_sleep", _skip)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _blocked_fixer(
    db_uri: str, store: SqlAlchemyConversationStore, *, launched_by: str | None
) -> str:
    """A foreman root owned by OWNER with a ``fixer`` child; returns the child id."""
    labels = {"omnigent.launched_by": launched_by} if launched_by else {}
    root = store.create_conversation(title="issues.opened: triage", labels=labels)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(OWNER)
    permissions.grant(OWNER, root.id, LEVEL_OWNER)
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        sub_agent_name="fixer",
        title="fixer:fix-issue-5",
    )
    return child.id


def _request(elicitation_id: str = "elicit_1") -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"message": "blast_radius: High-blast-radius command needs approval"},
    }


def _notifier(
    store: SqlAlchemyConversationStore, mailer: RecordingMailer, accounts: Accounts
) -> ApprovalEmailNotifier:
    return ApprovalEmailNotifier(
        conversation_store=store,
        account_store=accounts,
        mailer=mailer,
        base_url="https://app.example.com/",
        loop=asyncio.get_running_loop(),
    )


async def _settle(notifier: ApprovalEmailNotifier) -> None:
    """Wait for every scheduled grace handler to finish."""
    for _ in range(50):
        if not notifier._inflight:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("approval email handler did not finish")


@pytest.mark.asyncio
async def test_unanswered_approval_in_subscription_session_emails_owner(
    db_uri: str, conversation_store: SqlAlchemyConversationStore
) -> None:
    child_id = _blocked_fixer(db_uri, conversation_store, launched_by="subscription")
    mailer = RecordingMailer()
    notifier = _notifier(conversation_store, mailer, Accounts(root=VERIFIED))

    child = conversation_store.get_conversation(child_id)
    assert child is not None and child.parent_conversation_id is not None
    mirrored = _request()
    mirrored["params"] = {**mirrored["params"], "target_session_id": child_id}

    notifier.observe(child_id, _request())
    notifier.observe(child.parent_conversation_id, mirrored)  # copy in the foreman's chat
    notifier.observe(child_id, _request())  # re-published prompt
    await _settle(notifier)

    assert len(mailer.sent) == 1
    message = mailer.sent[0]
    assert message["to"] == "owner@example.com"
    assert message["subject"] == "Approval needed: fixer:fix-issue-5"
    assert f"https://app.example.com/approve/{child_id}/elicit_1" in message["text"]
    assert "High-blast-radius command" in message["text"]


@pytest.mark.asyncio
async def test_approval_answered_within_grace_sends_nothing(
    db_uri: str,
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_id = _blocked_fixer(db_uri, conversation_store, launched_by="subscription")
    mailer = RecordingMailer()
    notifier = _notifier(conversation_store, mailer, Accounts(root=VERIFIED))
    resolved = {"type": "response.elicitation_resolved", "elicitation_id": "elicit_1"}

    async def _answered_during_grace(seconds: float) -> None:
        del seconds
        notifier.observe(child_id, resolved)

    monkeypatch.setattr(approval_mailer, "_grace_sleep", _answered_during_grace)
    notifier.observe(child_id, _request())
    await _settle(notifier)

    assert mailer.sent == []


@pytest.mark.parametrize(
    "launched_by,account",
    [
        (None, VERIFIED),  # a person started it and is watching
        ("subscription", SimpleNamespace(email="owner@example.com", email_verified_at=None)),
        ("subscription", SimpleNamespace(email=None, email_verified_at=None)),
    ],
)
@pytest.mark.asyncio
async def test_no_email_for_attended_sessions_or_unverified_owners(
    db_uri: str,
    conversation_store: SqlAlchemyConversationStore,
    launched_by: str | None,
    account: SimpleNamespace,
) -> None:
    child_id = _blocked_fixer(db_uri, conversation_store, launched_by=launched_by)
    mailer = RecordingMailer()
    notifier = _notifier(conversation_store, mailer, Accounts(root=account))

    notifier.observe(child_id, _request())
    await _settle(notifier)

    assert mailer.sent == []


def test_fan_out_keeps_calling_observers_after_one_raises() -> None:
    seen: list[str] = []

    def _broken(conversation_id: str, event: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    def _recording(conversation_id: str, event: dict[str, Any]) -> None:
        seen.append(conversation_id)

    fan_out_observers(_broken, _recording)("conv_1", {})

    assert seen == ["conv_1"]
