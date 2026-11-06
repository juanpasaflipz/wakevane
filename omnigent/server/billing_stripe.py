"""Thin Stripe client boundary for seat billing.

Everything the billing policy needs from Stripe goes through
:class:`StripeGateway`, so tests substitute a fake and the ``stripe`` package
is imported only when billing is enabled (it ships as the ``billing`` extra).
The Stripe API version is pinned by the SDK release; subscription periods are
read from subscription items, where current API versions keep them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SubscriptionState:
    """The subscription facts the server mirrors."""

    id: str
    customer_id: str
    status: str
    seats: int
    period_start: int | None
    period_end: int | None
    metadata: Mapping[str, str] = field(default_factory=dict)


class StripeGateway(Protocol):
    """Operations the billing policy performs against Stripe."""

    def create_checkout_session(
        self,
        *,
        user_id: str,
        customer_id: str | None,
        price_seat: str,
        price_usage: str,
        seats: int,
        success_url: str,
        cancel_url: str,
        metadata: Mapping[str, str],
    ) -> str:
        """Return the hosted Checkout URL for a new seat subscription."""

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """Return the hosted customer portal URL."""

    def construct_event(self, payload: bytes, signature: str, secret: str) -> Mapping[str, Any]:
        """Verify a webhook signature and return the event as a mapping."""

    def retrieve_subscription(self, subscription_id: str) -> SubscriptionState:
        """Fetch a subscription's status, seats and current period."""

    def create_meter_event(
        self, *, event_name: str, customer_id: str, value: int, identifier: str
    ) -> str:
        """Report metered usage; ``identifier`` makes retries idempotent."""


def subscription_state(subscription: Mapping[str, Any]) -> SubscriptionState:
    """Normalize a Stripe subscription object into :class:`SubscriptionState`.

    Current API versions carry ``current_period_start``/``end`` on each
    subscription item; older ones carried them on the subscription itself.
    """
    items = ((subscription.get("items") or {}).get("data")) or []
    seats = 1
    period_start = subscription.get("current_period_start")
    period_end = subscription.get("current_period_end")
    for item in items:
        quantity = item.get("quantity")
        price = item.get("price") or {}
        if (
            isinstance(quantity, int)
            and (price.get("recurring") or {}).get("usage_type") != "metered"
        ):
            seats = max(1, quantity)
        period_start = period_start or item.get("current_period_start")
        period_end = period_end or item.get("current_period_end")
    customer = subscription.get("customer")
    if isinstance(customer, Mapping):
        customer = customer.get("id")
    metadata = {
        str(key): str(value) for key, value in (subscription.get("metadata") or {}).items()
    }
    return SubscriptionState(
        id=str(subscription["id"]),
        customer_id=str(customer),
        status=str(subscription.get("status") or "unknown"),
        seats=seats,
        period_start=int(period_start) if period_start else None,
        period_end=int(period_end) if period_end else None,
        metadata=metadata,
    )


class StripeSdkGateway:
    """Production gateway backed by ``stripe.StripeClient``.

    :param secret_key: Server-side Stripe secret key; never leaves the server.
    """

    def __init__(self, secret_key: str) -> None:
        import stripe  # pyrefly: ignore[missing-import]  # the optional ``billing`` extra

        self._stripe = stripe
        self._client = stripe.StripeClient(secret_key)

    def create_checkout_session(
        self,
        *,
        user_id: str,
        customer_id: str | None,
        price_seat: str,
        price_usage: str,
        seats: int,
        success_url: str,
        cancel_url: str,
        metadata: Mapping[str, str],
    ) -> str:
        params: dict[str, Any] = {
            "mode": "subscription",
            "client_reference_id": user_id,
            "line_items": [
                {"price": price_seat, "quantity": seats},
                # A metered price takes no quantity; usage arrives as meter events.
                {"price": price_usage},
            ],
            "metadata": dict(metadata),
            "subscription_data": {"metadata": dict(metadata)},
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if customer_id:
            params["customer"] = customer_id
        session = self._client.v1.checkout.sessions.create(params)
        return str(session.url)

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        session = self._client.v1.billing_portal.sessions.create(
            {"customer": customer_id, "return_url": return_url}
        )
        return str(session.url)

    def construct_event(self, payload: bytes, signature: str, secret: str) -> Mapping[str, Any]:
        event = self._client.construct_event(payload, signature, secret)
        return event.to_dict_recursive()

    def retrieve_subscription(self, subscription_id: str) -> SubscriptionState:
        subscription = self._client.v1.subscriptions.retrieve(subscription_id)
        return subscription_state(subscription.to_dict_recursive())

    def create_meter_event(
        self, *, event_name: str, customer_id: str, value: int, identifier: str
    ) -> str:
        event = self._client.v1.billing.meter_events.create(
            {
                "event_name": event_name,
                "identifier": identifier,
                "payload": {"stripe_customer_id": customer_id, "value": str(value)},
            }
        )
        return str(getattr(event, "identifier", identifier))
