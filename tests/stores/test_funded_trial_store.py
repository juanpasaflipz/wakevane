"""Trial grant admission and ledger invariants against real database transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.server.funded_trial_store import FundedTrialDenied, FundedTrialStore

_NOW = 1_800_000_000


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> float:
        return self.now


def _store(db_uri: str, clock: _Clock | None = None) -> FundedTrialStore:
    return FundedTrialStore(db_uri, clock=clock or _Clock())


def _grant(store: FundedTrialStore, user_id: str = "owner", **overrides: object) -> None:
    terms: dict[str, object] = {
        "allowance_micros": 100_000_000,
        "expires_at": _NOW + 30 * 86_400,
        "capabilities": ["inference", "compute"],
        "max_concurrent_roots": 1,
        "actor_id": "operator",
    }
    terms.update(overrides)
    store.upsert_grant(user_id, **terms)  # type: ignore[arg-type]


def _reserve(store: FundedTrialStore, key: str, amount: int, **overrides: object):
    arguments: dict[str, object] = {
        "capability": "inference",
        "kind": "inference",
        "amount_micros": amount,
        "idempotency_key": key,
        "root_session_id": "root-a",
    }
    arguments.update(overrides)
    return store.reserve("owner", **arguments)  # type: ignore[arg-type]


def test_account_without_grant_is_denied_and_audited(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    with pytest.raises(FundedTrialDenied) as denied:
        store.reserve(
            "someone-else",
            capability="inference",
            kind="inference",
            amount_micros=1,
            idempotency_key="k",
        )
    assert denied.value.reason == "no_grant"
    assert store.count_events("someone-else", "denied") == 1


def test_replayed_reservation_holds_allowance_once(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    first = _reserve(store, "request-1", 5_000_000)
    second = _reserve(store, "request-1", 5_000_000)
    assert first.id == second.id
    grant = store.get_grant("owner")
    assert grant is not None and grant.reserved_micros == 5_000_000


def test_settlement_is_idempotent_and_releases_the_hold(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    charge = _reserve(store, "request-1", 5_000_000)
    store.settle(charge.id, 1_250_000)
    replay = store.settle(charge.id, 9_000_000)
    assert replay.status == "settled" and replay.settled_micros == 1_250_000
    grant = store.get_grant("owner")
    assert grant is not None
    assert (grant.reserved_micros, grant.spent_micros) == (0, 1_250_000)
    assert grant.remaining_micros == 98_750_000


def test_overrun_is_recorded_not_hidden(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    charge = _reserve(store, "request-1", 1_000)
    store.settle(charge.id, 3_000)
    assert store.count_events("owner", "overrun") == 1
    grant = store.get_grant("owner")
    assert grant is not None and grant.spent_micros == 3_000


def test_concurrent_reservations_cannot_exceed_allowance(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store, allowance_micros=10_000_000)

    def attempt(index: int) -> bool:
        try:
            _reserve(store, f"request-{index}", 3_000_000)
        except FundedTrialDenied as denied:
            assert denied.reason == "allowance_exhausted"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = sum(pool.map(attempt, range(16)))
    assert admitted == 3
    grant = store.get_grant("owner")
    assert grant is not None and grant.reserved_micros == 9_000_000


def test_second_root_waits_for_the_first_to_settle(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    first = _reserve(store, "root-a-1", 1_000)
    _reserve(store, "root-a-child", 1_000)  # descendants share their root
    with pytest.raises(FundedTrialDenied) as denied:
        _reserve(store, "root-b-1", 1_000, root_session_id="root-b")
    assert denied.value.reason == "concurrency"
    store.settle(first.id, 500)
    for charge in store.open_charges("owner"):
        store.release(charge.id)
    assert _reserve(store, "root-b-2", 1_000, root_session_id="root-b").status == "reserved"


@pytest.mark.parametrize("stop", ["revoke", "expire", "capability"])
def test_revocation_expiry_and_capability_block_new_spend(db_uri: str, stop: str) -> None:
    clock = _Clock()
    store = _store(db_uri, clock)
    _grant(store)
    held = _reserve(store, "before", 1_000)
    expected = {"revoke": "revoked", "expire": "expired", "capability": "capability"}[stop]
    overrides: dict[str, object] = {}
    if stop == "revoke":
        store.revoke_grant("owner", actor_id="operator")
    elif stop == "expire":
        clock.now = _NOW + 31 * 86_400
    else:
        overrides = {"capability": "pull_request"}
    with pytest.raises(FundedTrialDenied) as denied:
        _reserve(store, "after", 1_000, **overrides)
    assert denied.value.reason == expected
    # Already-dispatched work can still be settled after the stop.
    assert store.settle(held.id, 700).status == "settled"


def test_regrant_keeps_recorded_spend(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    store.settle(_reserve(store, "request-1", 2_000_000).id, 2_000_000)
    store.revoke_grant("owner", actor_id="operator")
    _grant(store, allowance_micros=3_000_000)
    grant = store.get_grant("owner")
    assert grant is not None and grant.revoked_at is None
    assert grant.remaining_micros == 1_000_000
    with pytest.raises(FundedTrialDenied):
        _reserve(store, "request-2", 1_500_000)


def test_grant_kinds_are_keyed_by_period_and_carry_markup(db_uri: str) -> None:
    store = _store(db_uri)
    _grant(store)
    with pytest.raises(ValueError, match="kind"):
        _grant(store, kind="bonus")
    with pytest.raises(ValueError, match="billing period"):
        _grant(store, billing_period_end=_NOW + 100)
    first_period = _NOW + 10 * 86_400
    second_period = _NOW + 40 * 86_400
    credit = _grant_kind(store, "period_credit", first_period, allowance_micros=10_000_000)
    assert store.get_grant("owner") is not None  # the trial is untouched
    assert store.get_grant("owner", "period_credit") == credit
    charge = store.reserve(
        "owner",
        capability="inference",
        kind="inference",
        amount_micros=1_000_000,
        idempotency_key="marked-up",
        grant_id=credit.id,
    )
    assert charge.reserved_micros == 1_250_000  # 25 % markup on the hold
    settled = store.settle(charge.id, 400_000)
    assert settled.settled_micros == 500_000  # and on the measured cost
    later = _grant_kind(store, "period_credit", second_period, allowance_micros=10_000_000)
    assert later.id != credit.id and later.spent_micros == 0
    assert store.get_grant("owner", "period_credit") == later
    assert [grant.kind for grant in store.list_grants("owner")] == [
        "period_credit",
        "period_credit",
        "trial",
    ]
    revoked = store.revoke_grants("owner", kinds=("period_credit", "overage"), actor_id=None)
    assert {grant.id for grant in revoked} == {credit.id, later.id}
    assert store.get_grant("owner").revoked_at is None  # type: ignore[union-attr]


def test_reporting_marks_each_settled_overage_charge_once(db_uri: str) -> None:
    store = _store(db_uri)
    overage = _grant_kind(store, "overage", _NOW + 10 * 86_400, allowance_micros=10_000_000)
    charge = store.reserve(
        "owner",
        capability="inference",
        kind="inference",
        amount_micros=1_000_000,
        idempotency_key="o1",
        grant_id=overage.id,
    )
    assert store.settled_unreported(kind="overage") == []  # still reserved
    store.settle(charge.id, 800_000)
    pending = store.settled_unreported(kind="overage")
    assert [(item.id, user) for item, user in pending] == [(charge.id, "owner")]
    assert store.mark_reported(charge.id, report_ref="me_1") is True
    assert store.mark_reported(charge.id, report_ref="me_2") is False
    assert store.settled_unreported(kind="overage") == []
    assert store.get_charge(charge.id).report_ref == "me_1"  # type: ignore[union-attr]
    # Another account's grant cannot be charged through this grant id.
    with pytest.raises(FundedTrialDenied) as denied:
        store.reserve(
            "someone-else",
            capability="inference",
            kind="inference",
            amount_micros=1,
            idempotency_key="x",
            grant_id=overage.id,
        )
    assert denied.value.reason == "no_grant"


def _grant_kind(store: FundedTrialStore, kind: str, period_end: int, **overrides: object):
    terms: dict[str, object] = {
        "allowance_micros": 10_000_000,
        "expires_at": period_end,
        "capabilities": ["inference", "compute"],
        "max_concurrent_roots": 1,
        "actor_id": None,
        "kind": kind,
        "billing_period_end": period_end,
        "markup_percent": 25,
    }
    terms.update(overrides)
    return store.upsert_grant("owner", **terms)  # type: ignore[arg-type]
