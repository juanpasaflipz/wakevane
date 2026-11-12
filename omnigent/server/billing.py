"""Seat billing: subscription state, funding grants and Stripe synchronization.

Stripe is the system of record for money; WakeVane owns usage. A paid billing
period becomes a ``period_credit`` grant in the shared funded-trial ledger
(allowance = the plan's included credit, expiry = period end) and, when the
user allows it, an ``overage`` grant capped at their chosen amount whose
settled charges are reported to Stripe as meter events. Every hold and
settlement on those grants carries the configured markup.

Operator settings come from ``OMNIGENT_BILLING_CONFIG`` (JSON); the Stripe
secret and webhook signing secret are read from the environment variables it
names and never leave the server. The module is inert unless the ``billing``
release feature is on.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from omnigent.server.billing_store import BillingAccount, BillingStore
from omnigent.server.billing_stripe import StripeGateway, SubscriptionState, subscription_state
from omnigent.server.funded_trial_store import (
    CAPABILITIES,
    MICROS_PER_USD,
    FundedTrialStore,
    TrialGrant,
)

_logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "OMNIGENT_BILLING_CONFIG"
SUBSCRIPTION_GRANT_KINDS = ("period_credit", "overage")
_SEAT_STATUS_BY_STRIPE = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "canceled": "canceled",
    "incomplete_expired": "canceled",
    "incomplete": "none",
    "paused": "past_due",
}


def _usd_to_micros(value: float) -> int:
    micros = value * MICROS_PER_USD
    whole = int(micros)
    return whole if whole == micros else whole + 1


class BillingError(Exception):
    """A billing operation was refused; ``reason`` is a stable machine code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class PlanTerms:
    """One purchasable plan.

    :param price_seat: Stripe licensed monthly price id (quantity = seats).
    :param included_credit_micros: Usage credit granted each paid period.
    :param max_concurrent_roots: Root workflows that may run at once.
    :param min_seats: Smallest seat quantity Checkout offers.
    """

    key: str
    price_seat: str
    included_credit_micros: int
    max_concurrent_roots: int = 1
    min_seats: int = 1


@dataclass(frozen=True)
class BillingConfig:
    """Operator billing settings; see ``docs/SEAT_BILLING.md``.

    :param plans: Plan key → terms. The first plan is the default offer.
    :param price_usage: Stripe metered price id for overage (cents).
    :param meter_event_name: Stripe meter event name receiving overage cents.
    :param markup_percent: Added to list prices on subscription grants.
    :param default_overage_cap_micros: Cap offered when a user enables overage.
    :param grace_days: Informational dunning window shown to users.
    :param base_url: Public app URL for Checkout and portal return links.
    :param secret_key_env: Server env var holding the Stripe secret key.
    :param webhook_secret_env: Server env var holding the webhook signing secret.
    """

    plans: Mapping[str, PlanTerms] = field(default_factory=dict)
    price_usage: str = ""
    meter_event_name: str = "wakevane_overage_cents"
    markup_percent: int = 20
    default_overage_cap_micros: int = 50 * MICROS_PER_USD
    grace_days: int = 7
    base_url: str = ""
    secret_key_env: str = "OMNIGENT_BILLING_STRIPE_SECRET_KEY"
    webhook_secret_env: str = "OMNIGENT_BILLING_STRIPE_WEBHOOK_SECRET"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, base_url: str = "") -> BillingConfig:
        """Parse operator JSON; credits and caps are USD."""
        plans: dict[str, PlanTerms] = {}
        for key, terms in (raw.get("plans") or {}).items():
            plans[str(key)] = PlanTerms(
                key=str(key),
                price_seat=str(terms.get("price_seat") or ""),
                included_credit_micros=_usd_to_micros(float(terms.get("included_credit_usd", 0))),
                max_concurrent_roots=int(terms.get("max_concurrent_roots", 1)),
                min_seats=int(terms.get("min_seats", 1)),
            )
        defaults = cls()
        return cls(
            plans=plans,
            price_usage=str(raw.get("price_usage") or ""),
            meter_event_name=str(raw.get("meter_event_name", defaults.meter_event_name)),
            markup_percent=int(raw.get("markup_percent", defaults.markup_percent)),
            default_overage_cap_micros=_usd_to_micros(
                float(raw.get("default_overage_cap_usd", 50))
            ),
            grace_days=int(raw.get("grace_days", defaults.grace_days)),
            base_url=str(raw.get("base_url") or base_url).rstrip("/"),
            secret_key_env=str(raw.get("secret_key_env", defaults.secret_key_env)),
            webhook_secret_env=str(raw.get("webhook_secret_env", defaults.webhook_secret_env)),
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BillingConfig:
        """Read ``OMNIGENT_BILLING_CONFIG``; the app base URL fills in return links."""
        source = environ if environ is not None else os.environ
        text = source.get(CONFIG_ENV_VAR, "").strip()
        base_url = source.get("OMNIGENT_ACCOUNTS_BASE_URL", "").strip()
        import json

        return cls.from_mapping(json.loads(text) if text else {}, base_url=base_url)

    def validate(self) -> None:
        """Refuse to start with unpriced plans; mirrors the trial's unpriced-model rule."""
        if not self.plans:
            raise ValueError("OMNIGENT_BILLING_CONFIG must define at least one plan")
        for plan in self.plans.values():
            if not plan.price_seat:
                raise ValueError(f"Billing plan {plan.key!r} has no price_seat")
            if plan.max_concurrent_roots < 1 or plan.min_seats < 1:
                raise ValueError(f"Billing plan {plan.key!r} has invalid limits")
        if not self.price_usage:
            raise ValueError("OMNIGENT_BILLING_CONFIG must define price_usage")
        if self.markup_percent < 0:
            raise ValueError("Billing markup cannot be negative")
        if not self.base_url:
            raise ValueError("Billing needs a base_url (or OMNIGENT_ACCOUNTS_BASE_URL)")

    @property
    def default_plan(self) -> PlanTerms:
        return next(iter(self.plans.values()))


class BillingPolicy:
    """Subscription admission, Stripe flows and overage reporting for one deployment."""

    def __init__(
        self,
        *,
        enabled: bool,
        store: BillingStore | None,
        ledger: FundedTrialStore | None,
        config: BillingConfig,
        gateway: StripeGateway | None,
        environ: Mapping[str, str] | None = None,
        clock: Any = time.time,
    ) -> None:
        self.enabled = enabled
        self.store = store
        self.ledger = ledger
        self.config = config
        self.gateway = gateway
        self._environ = environ if environ is not None else os.environ
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock())

    def _require(self) -> tuple[BillingStore, FundedTrialStore, StripeGateway]:
        if not self.enabled or self.store is None or self.ledger is None:
            raise BillingError("disabled", "Seat billing is not enabled.")
        if self.gateway is None:
            raise BillingError("unconfigured", "The billing provider is not configured.")
        return self.store, self.ledger, self.gateway

    def _webhook_secret(self) -> str:
        secret = self._environ.get(self.config.webhook_secret_env, "")
        if not secret:
            raise BillingError("unconfigured", "The billing webhook secret is not configured.")
        return secret

    # ── Funding resolution ───────────────────────────────────────────

    def account(self, user_id: str) -> BillingAccount | None:
        if not self.enabled or self.store is None:
            return None
        return self.store.get_account(user_id)

    def has_live_subscription(self, user_id: str) -> bool:
        """Whether the payment provider is still billing a subscription for this user."""
        account = self.account(user_id)
        return (
            account is not None
            and bool(account.stripe_subscription_id)
            and account.seat_status not in {"none", "canceled"}
        )

    def funding_grants(self, user_id: str) -> list[TrialGrant]:
        """Subscription grants that may fund new work now, in spending order.

        Period credit is used first and stays usable while the period lasts,
        even when the seat is past due. Overage needs an active seat and the
        user's opt-in.
        """
        if not self.enabled or self.store is None or self.ledger is None:
            return []
        account = self.store.get_account(user_id)
        now = self._now()
        if account is None or not account.in_period(now):
            return []
        if account.seat_status not in {"active", "past_due"}:
            return []
        grants: list[TrialGrant] = []
        credit = self.ledger.get_grant(user_id, "period_credit")
        if (
            credit is not None
            and credit.billing_period_end == account.period_end
            and credit.inactive_reason(now) is None
        ):
            grants.append(credit)
        if account.seat_status == "active" and account.overage_enabled:
            overage = self.ledger.get_grant(user_id, "overage")
            if (
                overage is not None
                and overage.billing_period_end == account.period_end
                and overage.inactive_reason(now) is None
            ):
                grants.append(overage)
        return grants

    def denial_reason(self, user_id: str) -> str:
        """Why the account cannot spend on a subscription right now."""
        account = self.account(user_id)
        now = self._now()
        if account is None or account.seat_status in {"none", "canceled"}:
            return "subscription_required"
        if not account.in_period(now):
            return "subscription_required"
        if account.seat_status == "past_due":
            return "past_due"
        return "allowance_exhausted"

    # ── Caller-facing summary ─────────────────────────────────────────

    def summary(self, user_id: str) -> dict[str, Any]:
        """Plan, seat status, period, credit and overage figures for the caller."""
        plans = [
            {
                "key": plan.key,
                "included_credit_usd": plan.included_credit_micros / MICROS_PER_USD,
                "max_concurrent_roots": plan.max_concurrent_roots,
                "min_seats": plan.min_seats,
            }
            for plan in self.config.plans.values()
        ]
        base = {
            "enabled": self.enabled,
            "plans": plans,
            "markup_percent": self.config.markup_percent,
            "default_overage_cap_usd": self.config.default_overage_cap_micros / MICROS_PER_USD,
            "grace_days": self.config.grace_days,
        }
        account = self.account(user_id)
        if account is None or self.ledger is None:
            return {**base, "seat_status": "none", "plan": None}
        now = self._now()
        credit = self.ledger.get_grant(user_id, "period_credit")
        overage = self.ledger.get_grant(user_id, "overage")

        def figures(grant: TrialGrant | None) -> dict[str, Any] | None:
            if grant is None or grant.billing_period_end != account.period_end:
                return None
            return {
                "allowance_usd": grant.allowance_micros / MICROS_PER_USD,
                "spent_usd": grant.spent_micros / MICROS_PER_USD,
                "reserved_usd": grant.reserved_micros / MICROS_PER_USD,
                "remaining_usd": grant.remaining_micros / MICROS_PER_USD,
                "active": grant.inactive_reason(now) is None,
            }

        return {
            **base,
            "plan": account.plan,
            "seat_status": account.seat_status,
            "seats": account.seats,
            "period_start": account.period_start,
            "period_end": account.period_end,
            "in_period": account.in_period(now),
            "credit": figures(credit),
            "overage": {
                "enabled": account.overage_enabled,
                "cap_usd": account.overage_cap_micros / MICROS_PER_USD,
                **(figures(overage) or {}),
            },
            "portal_available": bool(account.stripe_customer_id),
            "recent_charges": [
                {
                    "id": charge.id,
                    "kind": charge.kind,
                    "status": charge.status,
                    "reserved_usd": charge.reserved_micros / MICROS_PER_USD,
                    "settled_usd": (
                        charge.settled_micros / MICROS_PER_USD
                        if charge.settled_micros is not None
                        else None
                    ),
                    "created_at": charge.created_at,
                }
                for charge in self.ledger.recent_charges(user_id, limit=10)
            ],
        }

    # ── Stripe flows ──────────────────────────────────────────────────

    def checkout_url(self, user_id: str, *, plan_key: str | None, seats: int | None) -> str:
        """Start a Checkout Session for a new seat subscription."""
        store, _, gateway = self._require()
        plan = self.config.plans.get(plan_key or self.config.default_plan.key)
        if plan is None:
            raise BillingError("unknown_plan", f"Unknown plan {plan_key!r}.")
        account = store.get_account(user_id)
        if account is not None and account.seat_status in {"active", "past_due"}:
            raise BillingError("already_subscribed", "This account already holds a seat.")
        quantity = max(plan.min_seats, seats or plan.min_seats)
        return gateway.create_checkout_session(
            user_id=user_id,
            customer_id=account.stripe_customer_id if account is not None else None,
            price_seat=plan.price_seat,
            price_usage=self.config.price_usage,
            seats=quantity,
            success_url=f"{self.config.base_url}/settings/billing?checkout=success",
            cancel_url=f"{self.config.base_url}/settings/billing?checkout=canceled",
            metadata={"wakevane_user_id": user_id, "wakevane_plan": plan.key},
        )

    def portal_url(self, user_id: str) -> str:
        """Open the customer portal for card, cancel and seat changes."""
        store, _, gateway = self._require()
        account = store.get_account(user_id)
        if account is None or not account.stripe_customer_id:
            raise BillingError("no_customer", "This account has no billing profile yet.")
        return gateway.create_portal_session(
            customer_id=account.stripe_customer_id,
            return_url=f"{self.config.base_url}/settings/billing",
        )

    def set_overage(self, user_id: str, *, enabled: bool, cap_usd: float | None) -> BillingAccount:
        """Change the account's overage opt-in and cap; new holds see it immediately."""
        store, ledger, _ = self._require()
        account = store.get_account(user_id)
        if account is None:
            raise BillingError("subscription_required", "Subscribe before enabling overage.")
        cap_micros = (
            _usd_to_micros(cap_usd)
            if cap_usd is not None
            else (account.overage_cap_micros or self.config.default_overage_cap_micros)
        )
        if cap_micros < 0:
            raise BillingError("invalid_cap", "The overage cap cannot be negative.")
        account = store.upsert_account(
            user_id, overage_cap_micros=cap_micros, overage_enabled=enabled
        )
        self._sync_overage_grant(account, ledger)
        return account

    # ── Webhooks ──────────────────────────────────────────────────────

    def handle_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        """Verify, record and process one Stripe event idempotently."""
        store, _, gateway = self._require()
        event = gateway.construct_event(payload, signature, self._webhook_secret())
        event_id = str(event["id"])
        event_type = str(event.get("type") or "")
        fresh = store.record_event(event_id, event_type=event_type)
        if not fresh and not store.unprocessed_event(event_id):
            return {"id": event_id, "type": event_type, "duplicate": True}
        try:
            handled = self._process(event_type, (event.get("data") or {}).get("object") or {})
        except Exception as exc:
            store.finish_event(event_id, error=f"{type(exc).__name__}: {exc}")
            raise
        store.finish_event(event_id, error=None)
        return {"id": event_id, "type": event_type, "handled": handled}

    def _process(self, event_type: str, obj: Mapping[str, Any]) -> bool:
        if event_type == "checkout.session.completed":
            return self._on_checkout_completed(obj)
        if event_type in {"invoice.paid", "invoice.payment_failed"}:
            return self._on_invoice(event_type, obj)
        if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
            return self._on_subscription(obj, deleted=event_type.endswith("deleted"))
        return False

    def _on_checkout_completed(self, session: Mapping[str, Any]) -> bool:
        if session.get("mode") != "subscription":
            return False
        _, _, gateway = self._require()
        user_id = session.get("client_reference_id")
        subscription_id = session.get("subscription")
        if not user_id or not subscription_id:
            raise BillingError("malformed_event", "Checkout session lacks user or subscription.")
        metadata = session.get("metadata") or {}
        state = gateway.retrieve_subscription(str(subscription_id))
        self.apply_subscription(
            str(user_id), state, plan_key=str(metadata.get("wakevane_plan") or "")
        )
        return True

    def _on_invoice(self, event_type: str, invoice: Mapping[str, Any]) -> bool:
        store, ledger, gateway = self._require()
        subscription_id = _invoice_subscription_id(invoice)
        if subscription_id is None:
            return False
        account = store.find_by_subscription(subscription_id)
        state = gateway.retrieve_subscription(subscription_id)
        user_id = (
            account.user_id if account is not None else state.metadata.get("wakevane_user_id")
        )
        if not user_id:
            _logger.warning("Ignoring %s for unknown subscription %s", event_type, subscription_id)
            return False
        plan_key = account.plan if account is not None else state.metadata.get("wakevane_plan", "")
        if event_type == "invoice.paid":
            self.apply_subscription(user_id, state, plan_key=plan_key)
            return True
        account = store.upsert_account(user_id, seat_status="past_due")
        # Period credit stays usable; metered overage stops until payment succeeds.
        ledger.revoke_grants(user_id, kinds=("overage",), actor_id=None)
        return True

    def _on_subscription(self, subscription: Mapping[str, Any], *, deleted: bool) -> bool:
        store, _, _ = self._require()
        state = subscription_state(subscription)
        if deleted:
            state = SubscriptionState(
                id=state.id,
                customer_id=state.customer_id,
                status="canceled",
                seats=state.seats,
                period_start=state.period_start,
                period_end=state.period_end,
                metadata=state.metadata,
            )
        account = store.find_by_subscription(state.id)
        user_id = (
            account.user_id if account is not None else state.metadata.get("wakevane_user_id")
        )
        if not user_id:
            return False
        plan_key = account.plan if account is not None else state.metadata.get("wakevane_plan", "")
        self.apply_subscription(user_id, state, plan_key=plan_key)
        return True

    def apply_subscription(
        self, user_id: str, state: SubscriptionState, *, plan_key: str
    ) -> BillingAccount:
        """Mirror a subscription into the account and its period grants.

        Safe to repeat: grants are keyed by period, so a redelivered event
        neither resets spend nor issues a second credit.
        """
        store, ledger, _ = self._require()
        plan = self.config.plans.get(plan_key) or (
            self.config.plans.get(store.get_account(user_id).plan)  # type: ignore[union-attr]
            if store.get_account(user_id) is not None
            else None
        )
        if plan is None:
            plan = self.config.default_plan
        seat_status = _SEAT_STATUS_BY_STRIPE.get(state.status, "none")
        existing = store.get_account(user_id)
        fields: dict[str, Any] = {
            "stripe_customer_id": state.customer_id,
            "stripe_subscription_id": state.id,
            "plan": plan.key,
            "seat_status": seat_status,
            "seats": max(1, state.seats),
        }
        if state.period_end is not None:
            fields["period_start"] = state.period_start
            fields["period_end"] = state.period_end
        if existing is None:
            fields["overage_cap_micros"] = self.config.default_overage_cap_micros
            fields["overage_enabled"] = False
        account = store.upsert_account(user_id, **fields)
        if seat_status == "canceled":
            ledger.revoke_grants(user_id, kinds=SUBSCRIPTION_GRANT_KINDS, actor_id=None)
            return account
        if seat_status == "active" and account.period_end and account.period_end > self._now():
            ledger.upsert_grant(
                user_id,
                kind="period_credit",
                billing_period_end=account.period_end,
                allowance_micros=plan.included_credit_micros,
                expires_at=account.period_end,
                capabilities=CAPABILITIES,
                max_concurrent_roots=plan.max_concurrent_roots,
                markup_percent=self.config.markup_percent,
                actor_id=None,
            )
            self._sync_overage_grant(account, ledger)
        return account

    def _sync_overage_grant(self, account: BillingAccount, ledger: FundedTrialStore) -> None:
        now = self._now()
        if (
            account.seat_status == "active"
            and account.overage_enabled
            and account.period_end
            and account.period_end > now
        ):
            plan = self.config.plans.get(account.plan) or self.config.default_plan
            ledger.upsert_grant(
                account.user_id,
                kind="overage",
                billing_period_end=account.period_end,
                allowance_micros=account.overage_cap_micros,
                expires_at=account.period_end,
                capabilities=CAPABILITIES,
                max_concurrent_roots=plan.max_concurrent_roots,
                markup_percent=self.config.markup_percent,
                actor_id=None,
            )
        else:
            ledger.revoke_grants(account.user_id, kinds=("overage",), actor_id=None)

    # ── Overage reporting ─────────────────────────────────────────────

    def report_overage(self, *, limit: int = 200) -> int:
        """Send settled overage charges to Stripe as meter events; returns the count.

        Each charge is reported once: the meter event identifier is the charge
        id, and the charge is marked reported only after Stripe accepts it.
        """
        store, ledger, gateway = self._require()
        reported = 0
        for charge, user_id in ledger.settled_unreported(kind="overage", limit=limit):
            account = store.get_account(user_id)
            if account is None or not account.stripe_customer_id:
                _logger.error(
                    "Overage charge %s has no billing customer; left unreported", charge.id
                )
                continue
            cents = -(-(charge.settled_micros or 0) // 10_000)
            if cents <= 0:
                ledger.mark_reported(charge.id, report_ref="zero")
                continue
            reference = gateway.create_meter_event(
                event_name=self.config.meter_event_name,
                customer_id=account.stripe_customer_id,
                value=cents,
                identifier=charge.id,
            )
            if ledger.mark_reported(charge.id, report_ref=reference):
                reported += 1
        return reported


def _invoice_subscription_id(invoice: Mapping[str, Any]) -> str | None:
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    subscription = details.get("subscription") or invoice.get("subscription")
    if isinstance(subscription, Mapping):
        subscription = subscription.get("id")
    return str(subscription) if subscription else None
