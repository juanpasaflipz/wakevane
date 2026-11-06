"""Seat billing: subscription funding order, Stripe webhooks, overage reporting and routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.billing import BillingConfig, BillingError, BillingPolicy
from omnigent.server.billing_store import BillingStore
from omnigent.server.billing_stripe import SubscriptionState, subscription_state
from omnigent.server.funded_trial import FundedTrialConfig, FundedTrialPolicy, denial_error_code
from omnigent.server.funded_trial_store import MICROS_PER_USD, FundedTrialDenied, FundedTrialStore
from omnigent.server.routes.billing import create_billing_router

_NOW = 1_800_000_000
_PERIOD_END = _NOW + 30 * 86_400
_CONFIG = BillingConfig.from_mapping(
    {
        "plans": {
            "pro": {"price_seat": "price_seat_test", "included_credit_usd": 20},
            "team": {
                "price_seat": "price_team_test",
                "included_credit_usd": 20,
                "max_concurrent_roots": 3,
                "min_seats": 3,
            },
        },
        "price_usage": "price_usage_test",
        "markup_percent": 20,
        "default_overage_cap_usd": 50,
        "base_url": "https://app.wakevane.test",
    }
)
_TRIAL_CONFIG = FundedTrialConfig.from_mapping(
    {
        "models": {
            "claude-test": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
        },
        "compute_usd_per_hour": {"cloud-test": 0.5},
    }
)
_ENVIRON = {
    "OMNIGENT_BILLING_STRIPE_WEBHOOK_SECRET": "whsec_test",
    "OMNIGENT_FUNDED_TRIAL_SIGNING_KEY": "s" * 40,
}


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> float:
        return self.now


class _FakeStripe:
    """Records what the policy asks of Stripe and replays canned objects."""

    def __init__(self) -> None:
        self.checkouts: list[dict[str, Any]] = []
        self.portals: list[dict[str, Any]] = []
        self.meter_events: list[dict[str, Any]] = []
        self.subscriptions: dict[str, SubscriptionState] = {}
        self.valid_signature = "sig-ok"

    def create_checkout_session(self, **params: Any) -> str:
        self.checkouts.append(params)
        return "https://checkout.stripe.test/cs_1"

    def create_portal_session(self, **params: Any) -> str:
        self.portals.append(params)
        return "https://portal.stripe.test/bps_1"

    def construct_event(self, payload: bytes, signature: str, secret: str) -> Mapping[str, Any]:
        if signature != self.valid_signature or secret != "whsec_test":
            raise ValueError("bad signature")
        return json.loads(payload)

    def retrieve_subscription(self, subscription_id: str) -> SubscriptionState:
        return self.subscriptions[subscription_id]

    def create_meter_event(self, **params: Any) -> str:
        self.meter_events.append(params)
        return "me_" + str(params["identifier"])


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


class _Accounts:
    def is_admin(self, user_id: str) -> bool:
        return user_id == "operator"


def _subscription(
    status: str = "active", *, seats: int = 1, period_end: int = _PERIOD_END
) -> SubscriptionState:
    return SubscriptionState(
        id="sub_1",
        customer_id="cus_1",
        status=status,
        seats=seats,
        period_start=_NOW,
        period_end=period_end,
        metadata={"wakevane_user_id": "owner", "wakevane_plan": "pro"},
    )


def _event(event_type: str, obj: dict[str, Any], event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


@pytest.fixture
def billing(db_uri: str) -> dict[str, Any]:
    clock = _Clock()
    ledger = FundedTrialStore(db_uri, clock=clock)
    stripe = _FakeStripe()
    stripe.subscriptions["sub_1"] = _subscription()
    policy = BillingPolicy(
        enabled=True,
        store=BillingStore(db_uri, clock=clock),
        ledger=ledger,
        config=_CONFIG,
        gateway=stripe,
        environ=_ENVIRON,
        clock=clock,
    )
    trial = FundedTrialPolicy(
        enabled=True,
        store=ledger,
        config=_TRIAL_CONFIG,
        environ=_ENVIRON,
        clock=clock,
        billing=policy,
    )
    return {"policy": policy, "trial": trial, "ledger": ledger, "stripe": stripe, "clock": clock}


def _checkout_completed(policy: BillingPolicy, event_id: str = "evt_checkout") -> dict[str, Any]:
    return policy.handle_webhook(
        _event(
            "checkout.session.completed",
            {
                "mode": "subscription",
                "client_reference_id": "owner",
                "subscription": "sub_1",
                "customer": "cus_1",
                "metadata": {"wakevane_plan": "pro"},
            },
            event_id,
        ),
        "sig-ok",
    )


def _reserve(trial: FundedTrialPolicy, key: str, usd: float, root: str = "root-1") -> Any:
    return trial.reserve(
        "owner",
        capability="inference",
        kind="inference",
        amount_micros=int(usd * MICROS_PER_USD),
        idempotency_key=key,
        root_session_id=root,
    )


# ── Config ────────────────────────────────────────────────────────────


def test_config_refuses_unpriced_plans() -> None:
    with pytest.raises(ValueError, match="price_seat"):
        BillingConfig.from_mapping(
            {"plans": {"pro": {"included_credit_usd": 20}}, "price_usage": "p"}, base_url="x"
        ).validate()
    with pytest.raises(ValueError, match="price_usage"):
        BillingConfig.from_mapping(
            {"plans": {"pro": {"price_seat": "p", "included_credit_usd": 20}}}, base_url="x"
        ).validate()
    _CONFIG.validate()


def test_subscription_state_reads_item_periods_and_seats() -> None:
    state = subscription_state(
        {
            "id": "sub_9",
            "customer": {"id": "cus_9"},
            "status": "active",
            "metadata": {"wakevane_user_id": "u"},
            "items": {
                "data": [
                    {
                        "quantity": 4,
                        "price": {"recurring": {"usage_type": "licensed"}},
                        "current_period_start": 10,
                        "current_period_end": 20,
                    },
                    {"price": {"recurring": {"usage_type": "metered"}}},
                ]
            },
        }
    )
    assert (state.customer_id, state.seats, state.period_start, state.period_end) == (
        "cus_9",
        4,
        10,
        20,
    )
    assert state.metadata == {"wakevane_user_id": "u"}


# ── Subscription lifecycle ─────────────────────────────────────────────


def test_checkout_completion_creates_the_period_credit(billing: dict[str, Any]) -> None:
    policy, ledger = billing["policy"], billing["ledger"]
    result = _checkout_completed(policy)
    assert result["handled"] is True
    account = policy.account("owner")
    assert account is not None and account.seat_status == "active"
    assert account.period_end == _PERIOD_END and not account.overage_enabled
    credit = ledger.get_grant("owner", "period_credit")
    assert credit is not None
    assert credit.allowance_micros == 20 * MICROS_PER_USD
    assert credit.expires_at == _PERIOD_END and credit.markup_percent == 20
    assert ledger.get_grant("owner", "overage") is None
    # Redelivery is a no-op: same grant, no second credit, no spend reset.
    ledger.settle(_reserve(billing["trial"], "k1", 1.0).id, 1_000_000)
    replay = _checkout_completed(policy)
    assert replay["duplicate"] is True
    again = ledger.get_grant("owner", "period_credit")
    assert again is not None and again.id == credit.id and again.spent_micros == 1_200_000


def test_webhook_rejects_bad_signatures_and_records_failures(billing: dict[str, Any]) -> None:
    policy = billing["policy"]
    with pytest.raises(ValueError):
        policy.handle_webhook(_event("invoice.paid", {}), "sig-bad")
    assert policy.account("owner") is None
    # An event whose processing fails stays unprocessed, so a redelivery retries it.
    billing["stripe"].subscriptions.pop("sub_1")
    with pytest.raises(KeyError):
        _checkout_completed(policy, "evt_retry")
    billing["stripe"].subscriptions["sub_1"] = _subscription()
    assert _checkout_completed(policy, "evt_retry")["handled"] is True


def test_new_period_resets_credit_without_touching_the_old_ledger(
    billing: dict[str, Any],
) -> None:
    policy, ledger, trial, clock = (
        billing["policy"],
        billing["ledger"],
        billing["trial"],
        billing["clock"],
    )
    _checkout_completed(policy)
    first = ledger.get_grant("owner", "period_credit")
    assert first is not None
    ledger.settle(_reserve(trial, "k1", 5.0).id, 5_000_000)
    next_end = _PERIOD_END + 30 * 86_400
    clock.now = _PERIOD_END + 10
    billing["stripe"].subscriptions["sub_1"] = _subscription(period_end=next_end)
    policy.handle_webhook(
        _event(
            "invoice.paid",
            {"parent": {"subscription_details": {"subscription": "sub_1"}}},
            "evt_invoice_2",
        ),
        "sig-ok",
    )
    second = ledger.get_grant("owner", "period_credit")
    assert second is not None and second.id != first.id
    assert second.spent_micros == 0 and second.billing_period_end == next_end
    assert ledger.get_grant_by_id(first.id).spent_micros == 6_000_000  # type: ignore[union-attr]
    assert policy.summary("owner")["credit"]["remaining_usd"] == 20.0


def test_payment_failure_and_cancellation_stop_spending(billing: dict[str, Any]) -> None:
    policy, ledger, trial = billing["policy"], billing["ledger"], billing["trial"]
    _checkout_completed(policy)
    policy.set_overage("owner", enabled=True, cap_usd=10)
    assert ledger.get_grant("owner", "overage") is not None
    policy.handle_webhook(
        _event("invoice.payment_failed", {"subscription": "sub_1"}, "evt_fail"), "sig-ok"
    )
    assert policy.account("owner").seat_status == "past_due"  # type: ignore[union-attr]
    # Period credit stays usable while the period lasts; overage is revoked.
    assert _reserve(trial, "k-credit", 1.0).status == "reserved"
    assert [grant.kind for grant in trial.candidate_grants("owner")] == ["period_credit"]
    policy.handle_webhook(
        _event(
            "customer.subscription.deleted",
            {"id": "sub_1", "customer": "cus_1", "status": "canceled", "items": {"data": []}},
            "evt_deleted",
        ),
        "sig-ok",
    )
    assert policy.account("owner").seat_status == "canceled"  # type: ignore[union-attr]
    with pytest.raises(FundedTrialDenied) as denied:
        _reserve(trial, "k-after", 1.0)
    assert denied.value.reason == "subscription_required"
    assert denial_error_code(denied.value.reason) == "payment_required"


# ── Funding order ─────────────────────────────────────────────────────


def test_trial_then_credit_then_overage_fund_holds_in_order(billing: dict[str, Any]) -> None:
    policy, ledger, trial = billing["policy"], billing["ledger"], billing["trial"]
    ledger.upsert_grant(
        "owner",
        allowance_micros=1 * MICROS_PER_USD,
        expires_at=_PERIOD_END,
        capabilities=["inference", "compute"],
        max_concurrent_roots=1,
        actor_id="operator",
    )
    _checkout_completed(policy)
    policy.set_overage("owner", enabled=True, cap_usd=30)
    kinds = [grant.kind for grant in trial.candidate_grants("owner")]
    assert kinds == ["trial", "period_credit", "overage"]
    # $0.50 fits the trial at list price; trial grants carry no markup.
    trial_charge = _reserve(trial, "k1", 0.5)
    assert ledger.get_grant_by_id(trial_charge.grant_id).kind == "trial"  # type: ignore[union-attr]
    assert trial_charge.reserved_micros == 500_000
    # $1 no longer fits the trial, so the whole hold moves to period credit with markup.
    credit_charge = _reserve(trial, "k2", 1.0)
    assert ledger.get_grant_by_id(credit_charge.grant_id).kind == "period_credit"  # type: ignore[union-attr]
    assert credit_charge.reserved_micros == 1_200_000
    # $18 (+20 % = $21.60) exceeds the $18.80 of remaining credit; overage takes
    # the whole hold, never a split.
    overage_charge = _reserve(trial, "k3", 18.0)
    assert ledger.get_grant_by_id(overage_charge.grant_id).kind == "overage"  # type: ignore[union-attr]
    assert overage_charge.reserved_micros == 21_600_000
    # Beyond the overage cap everything is refused with a payment reason.
    with pytest.raises(FundedTrialDenied) as denied:
        _reserve(trial, "k4", 18.0)
    assert denied.value.reason == "allowance_exhausted"
    # Replaying a key returns the charge from whichever grant holds it.
    assert _reserve(trial, "k3", 18.0).id == overage_charge.id
    # Settlement applies the same markup and reporting picks up overage only.
    ledger.settle(overage_charge.id, 1_000_000)
    ledger.settle(credit_charge.id, 1_000_000)
    assert policy.report_overage() == 1
    event = billing["stripe"].meter_events[0]
    assert event == {
        "event_name": "wakevane_overage_cents",
        "customer_id": "cus_1",
        "value": 120,
        "identifier": overage_charge.id,
    }
    assert policy.report_overage() == 0
    assert ledger.get_charge(overage_charge.id).report_ref == "me_" + overage_charge.id  # type: ignore[union-attr]


def test_concurrency_counts_roots_across_grants(billing: dict[str, Any]) -> None:
    policy, trial = billing["policy"], billing["trial"]
    _checkout_completed(policy)
    policy.set_overage("owner", enabled=True, cap_usd=50)
    _reserve(trial, "a", 19.0, root="root-a")  # credit nearly exhausted
    with pytest.raises(FundedTrialDenied) as denied:
        _reserve(trial, "b", 5.0, root="root-b")  # would land on overage
    assert denied.value.reason == "concurrency"


def test_overage_opt_in_and_cap_change(billing: dict[str, Any]) -> None:
    policy, ledger = billing["policy"], billing["ledger"]
    with pytest.raises(BillingError) as refused:
        policy.set_overage("owner", enabled=True, cap_usd=10)
    assert refused.value.reason == "subscription_required"
    _checkout_completed(policy)
    account = policy.set_overage("owner", enabled=True, cap_usd=None)
    assert account.overage_cap_micros == 50 * MICROS_PER_USD
    overage = ledger.get_grant("owner", "overage")
    assert overage is not None and overage.allowance_micros == 50 * MICROS_PER_USD
    policy.set_overage("owner", enabled=True, cap_usd=12.5)
    assert ledger.get_grant("owner", "overage").allowance_micros == 12_500_000  # type: ignore[union-attr]
    policy.set_overage("owner", enabled=False, cap_usd=None)
    assert ledger.get_grant("owner", "overage").revoked_at is not None  # type: ignore[union-attr]
    assert policy.summary("owner")["overage"]["enabled"] is False


def test_gateway_token_survives_period_rollover_but_not_cancellation(
    billing: dict[str, Any],
) -> None:
    policy, trial, clock = billing["policy"], billing["trial"], billing["clock"]
    _checkout_completed(policy)
    token = trial.mint_gateway_token("owner", root_session_id="r", session_id="s")
    assert trial.verify_gateway_token(token).user_id == "owner"
    next_end = _PERIOD_END + 30 * 86_400
    billing["stripe"].subscriptions["sub_1"] = _subscription(period_end=next_end)
    policy.handle_webhook(_event("invoice.paid", {"subscription": "sub_1"}, "evt_roll"), "sig-ok")
    assert trial.verify_gateway_token(token).user_id == "owner"
    policy.handle_webhook(
        _event(
            "customer.subscription.deleted",
            {"id": "sub_1", "customer": "cus_1", "status": "canceled", "items": {"data": []}},
            "evt_cancel",
        ),
        "sig-ok",
    )
    with pytest.raises(FundedTrialDenied):
        trial.verify_gateway_token(token)
    clock.now = _NOW  # unchanged; the denial came from the revoked grants, not expiry


# ── Routes ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(billing: dict[str, Any]) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status, content={"message": exc.message, "code": exc.code}
        )

    app.include_router(
        create_billing_router(
            policy=billing["policy"],
            auth_provider=_HeaderAuth(),  # type: ignore[arg-type]
            permission_store=_Accounts(),
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wakevane.test"
    ) as http:
        yield http


@pytest.mark.asyncio
async def test_routes_require_a_caller_and_drive_checkout_and_portal(
    client: httpx.AsyncClient, billing: dict[str, Any]
) -> None:
    assert (await client.get("/v1/billing")).status_code == 401
    owner = {"x-test-user": "owner"}
    summary = await client.get("/v1/billing", headers=owner)
    assert summary.status_code == 200 and summary.json()["seat_status"] == "none"
    assert [plan["key"] for plan in summary.json()["plans"]] == ["pro", "team"]
    checkout = await client.post("/v1/billing/checkout", json={"plan": "team"}, headers=owner)
    assert checkout.status_code == 200 and checkout.json()["url"].startswith("https://checkout")
    request = billing["stripe"].checkouts[0]
    assert request["price_seat"] == "price_team_test" and request["seats"] == 3
    assert request["metadata"] == {"wakevane_user_id": "owner", "wakevane_plan": "team"}
    assert (
        await client.post("/v1/billing/checkout", json={"plan": "nope"}, headers=owner)
    ).status_code == 400
    assert (await client.post("/v1/billing/portal", headers=owner)).status_code == 404
    _checkout_completed(billing["policy"])
    assert (await client.post("/v1/billing/checkout", json={}, headers=owner)).status_code == 409
    portal = await client.post("/v1/billing/portal", headers=owner)
    assert portal.status_code == 200 and billing["stripe"].portals[0]["customer_id"] == "cus_1"
    overage = await client.put(
        "/v1/billing/overage", json={"enabled": True, "cap_usd": 25}, headers=owner
    )
    assert overage.status_code == 200 and overage.json()["overage"]["cap_usd"] == 25.0
    admin = await client.get(
        "/v1/admin/billing/accounts/owner", headers={"x-test-user": "operator"}
    )
    assert admin.status_code == 200 and admin.json()["account"]["seat_status"] == "active"
    assert (await client.get("/v1/admin/billing/accounts/owner", headers=owner)).status_code == 403


@pytest.mark.asyncio
async def test_webhook_route_answers_stripe(client: httpx.AsyncClient) -> None:
    body = _event("invoice.paid", {"subscription": "sub_1"}, "evt_route")
    bad = await client.post(
        "/v1/billing/webhooks/stripe", content=body, headers={"stripe-signature": "nope"}
    )
    assert bad.status_code == 400
    good = await client.post(
        "/v1/billing/webhooks/stripe", content=body, headers={"stripe-signature": "sig-ok"}
    )
    assert good.status_code == 200 and good.json()["handled"] is True
    again = await client.post(
        "/v1/billing/webhooks/stripe", content=body, headers={"stripe-signature": "sig-ok"}
    )
    assert again.json()["duplicate"] is True
