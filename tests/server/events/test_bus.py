"""Tests for the server event bus (``omnigent.server.events.bus``).

Covers the pure matching rule, the cooldown window, delivery-failure
isolation and ``record_fire`` stamping, all against an in-memory fake store
so no database is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities import Subscription
from omnigent.server.events import (
    CanonicalEvent,
    EventBus,
    PublishResult,
    event_matches,
    format_wake_notice,
)
from omnigent.server.events.bus import WAKE_NOTICE_FIELDS_MAX_CHARS


class FakeSubscriptionStore:
    """In-memory stand-in exposing only what the bus calls."""

    def __init__(self, subscriptions: list[Subscription]) -> None:
        self.rows = {subscription.id: subscription for subscription in subscriptions}
        self.fires: list[tuple[str, int]] = []

    def list_active(self, source: str, kind: str) -> list[Subscription]:
        return [
            row
            for row in self.rows.values()
            if row.state == "active" and row.source == source and row.kind == kind
        ]

    def record_fire(self, subscription_id: str, fired_at: int) -> Subscription | None:
        row = self.rows[subscription_id]
        row.last_fired_at = fired_at
        row.fire_count += 1
        self.fires.append((subscription_id, fired_at))
        return row


class RecordingDelivery:
    """Collects every wake notice the bus hands over, optionally failing."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_for = fail_for or set()

    async def __call__(self, subscription: Subscription, text: str) -> None:
        self.calls.append((subscription.id, text))
        if subscription.id in self.fail_for:
            raise RuntimeError("runner unreachable")


def _subscription(
    subscription_id: str,
    *,
    match: dict[str, Any] | None = None,
    source: str = "github",
    kind: str = "check_run.completed",
    state: str = "active",
    last_fired_at: int | None = None,
    note: str | None = "CI on my PR",
) -> Subscription:
    return Subscription(
        id=subscription_id,
        user_id="alice@example.com",
        conversation_id=f"conv_{subscription_id}",
        source=source,
        kind=kind,
        created_at=1_000,
        match=match or {},
        note=note,
        state=state,
        last_fired_at=last_fired_at,
    )


def _event(fields: dict[str, Any] | None = None, **overrides: Any) -> CanonicalEvent:
    return CanonicalEvent(
        source=overrides.get("source", "github"),
        kind=overrides.get("kind", "check_run.completed"),
        fields=fields if fields is not None else {"repo": "acme/api", "conclusion": "failure"},
        received_at=5_000,
    )


# ── event_matches ────────────────────────────────────────────────────────────


def test_empty_match_accepts_any_event() -> None:
    assert event_matches({}, {"repo": "acme/api"}) is True


def test_subset_match_requires_every_key_with_equal_value() -> None:
    fields = {"repo": "acme/api", "conclusion": "failure", "pull_request": 412}
    assert event_matches({"repo": "acme/api", "conclusion": "failure"}, fields) is True
    assert event_matches({"repo": "acme/api", "conclusion": "success"}, fields) is False
    # A filter key absent from the event never matches.
    assert event_matches({"repo": "acme/api", "name": "unit"}, fields) is False


def test_int_and_str_values_compare_as_strings() -> None:
    assert event_matches({"pull_request": "412"}, {"pull_request": 412}) is True
    assert event_matches({"pull_request": 412}, {"pull_request": "412"}) is True
    assert event_matches({"pull_request": 412}, {"pull_request": 413}) is False


def test_bool_values_do_not_coerce_to_strings() -> None:
    # ``True`` is an int subclass; it must not match the string "True" or 1.
    assert event_matches({"draft": True}, {"draft": "True"}) is False
    assert event_matches({"draft": True}, {"draft": True}) is True


# ── publish ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_delivers_matching_subscriptions_and_stamps_fire() -> None:
    store = FakeSubscriptionStore(
        [
            _subscription("match_repo", match={"repo": "acme/api"}),
            _subscription("match_pr", match={"pull_request": "412"}),
            _subscription("other_repo", match={"repo": "acme/web"}),
            _subscription("other_kind", kind="push"),
            _subscription("paused", state="paused"),
        ]
    )
    delivery = RecordingDelivery()
    bus = EventBus(store, delivery, clock=lambda: 10_000)

    result = await bus.publish(
        _event({"repo": "acme/api", "conclusion": "failure", "pull_request": 412})
    )

    assert result == PublishResult(matched=2, delivered=2, suppressed=0)
    assert [call[0] for call in delivery.calls] == ["match_repo", "match_pr"]
    assert store.fires == [("match_repo", 10_000), ("match_pr", 10_000)]
    assert store.rows["match_repo"].fire_count == 1
    assert store.rows["other_repo"].fire_count == 0


@pytest.mark.asyncio
async def test_publish_wake_text_carries_header_and_compact_fields() -> None:
    store = FakeSubscriptionStore([_subscription("sub1", match={"repo": "acme/api"})])
    delivery = RecordingDelivery()
    bus = EventBus(store, delivery, clock=lambda: 10_000)

    await bus.publish(_event({"repo": "acme/api", "conclusion": "failure", "pull_request": 412}))

    ((_, text),) = delivery.calls
    header, payload = text.split("\n", 1)
    assert header == (
        "[System: event github/check_run.completed matched subscription sub1 ('CI on my PR')]"
    )
    assert payload == '{"conclusion":"failure","pull_request":412,"repo":"acme/api"}'


def test_format_wake_notice_truncates_large_payloads() -> None:
    event = _event({"blob": "x" * (WAKE_NOTICE_FIELDS_MAX_CHARS * 2)})
    text = format_wake_notice(event, _subscription("sub1"))
    _, payload = text.split("\n", 1)
    assert payload.endswith("…[truncated]")
    assert len(payload) == WAKE_NOTICE_FIELDS_MAX_CHARS + len("…[truncated]")


@pytest.mark.asyncio
async def test_cooldown_suppresses_recent_refire_but_not_stale_one() -> None:
    store = FakeSubscriptionStore(
        [
            _subscription("recent", last_fired_at=9_995),
            _subscription("stale", last_fired_at=9_000),
            _subscription("never"),
        ]
    )
    delivery = RecordingDelivery()
    bus = EventBus(store, delivery, cooldown_seconds=10, clock=lambda: 10_000)

    result = await bus.publish(_event())

    assert result == PublishResult(matched=3, delivered=2, suppressed=1)
    assert [call[0] for call in delivery.calls] == ["stale", "never"]
    # A suppressed match is not stamped, so its cooldown window does not slide.
    assert store.rows["recent"].last_fired_at == 9_995
    assert store.rows["recent"].fire_count == 0


@pytest.mark.asyncio
async def test_delivery_failure_is_logged_not_raised_and_others_still_deliver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeSubscriptionStore([_subscription("broken"), _subscription("fine")])
    delivery = RecordingDelivery(fail_for={"broken"})
    bus = EventBus(store, delivery, clock=lambda: 10_000)

    with caplog.at_level("ERROR", logger="omnigent.server.events.bus"):
        result = await bus.publish(_event())

    assert result == PublishResult(matched=2, delivered=1, suppressed=0)
    assert [call[0] for call in delivery.calls] == ["broken", "fine"]
    assert any("broken" in record.getMessage() for record in caplog.records)
    # The fire is stamped before delivery so a retry storm still respects cooldown.
    assert store.rows["broken"].fire_count == 1


@pytest.mark.asyncio
async def test_publish_with_no_subscriptions_returns_zero_counts() -> None:
    bus = EventBus(FakeSubscriptionStore([]), RecordingDelivery())
    assert await bus.publish(_event()) == PublishResult()


@pytest.mark.asyncio
async def test_listeners_see_every_event_and_a_failing_one_is_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []

    async def broken(event: CanonicalEvent) -> None:
        raise RuntimeError("listener exploded")

    async def recording(event: CanonicalEvent) -> None:
        seen.append(event.kind)

    bus = EventBus(FakeSubscriptionStore([]), RecordingDelivery(), listeners=[broken])
    bus.add_listener(recording)

    with caplog.at_level("ERROR", logger="omnigent.server.events.bus"):
        result = await bus.publish(_event())

    # No subscription matched, yet the listeners still ran.
    assert result == PublishResult()
    assert seen == [_event().kind]
    assert any("listener failed" in record.getMessage() for record in caplog.records)
