#!/usr/bin/env python3
"""Create the Stripe objects WakeVane seat billing needs and print its config.

Idempotent by lookup key: rerunning against the same account reuses the
product, prices and meter it created before. Requires the ``stripe`` package
(``uv pip install 'omnigent[billing]'``) and ``STRIPE_SECRET_KEY`` in the
environment; use a test-mode key first.

Usage::

    STRIPE_SECRET_KEY=sk_test_... python scripts/billing/bootstrap_stripe.py \
        --pro-usd 29 --team-usd 25 --included-credit-usd 20

Then set ``OMNIGENT_BILLING_CONFIG`` to the JSON it prints, register the
webhook endpoint it names, and copy that endpoint's signing secret into
``OMNIGENT_BILLING_STRIPE_WEBHOOK_SECRET``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

METER_EVENT_NAME = "wakevane_overage_cents"
WEBHOOK_EVENTS = (
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


def _find_price(client, lookup_key: str):
    prices = client.v1.prices.list({"lookup_keys": [lookup_key], "limit": 1})
    return prices.data[0] if prices.data else None


def _ensure_product(client, name: str, lookup: str):
    products = client.v1.products.search({"query": f"metadata['wakevane']:'{lookup}'"})
    if products.data:
        return products.data[0]
    return client.v1.products.create({"name": name, "metadata": {"wakevane": lookup}})


def _ensure_seat_price(client, product_id: str, lookup_key: str, usd: float):
    existing = _find_price(client, lookup_key)
    if existing is not None:
        return existing
    return client.v1.prices.create(
        {
            "product": product_id,
            "currency": "usd",
            "unit_amount": round(usd * 100),
            "recurring": {"interval": "month", "usage_type": "licensed"},
            "lookup_key": lookup_key,
            "transfer_lookup_key": True,
        }
    )


def _ensure_meter(client):
    meters = client.v1.billing.meters.list({"status": "active", "limit": 100})
    for meter in meters.data:
        if meter.event_name == METER_EVENT_NAME:
            return meter
    return client.v1.billing.meters.create(
        {
            "display_name": "WakeVane overage (cents)",
            "event_name": METER_EVENT_NAME,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"},
        }
    )


def _ensure_usage_price(client, product_id: str, meter_id: str):
    lookup_key = "wakevane_overage_cents_v1"
    existing = _find_price(client, lookup_key)
    if existing is not None:
        return existing
    return client.v1.prices.create(
        {
            "product": product_id,
            "currency": "usd",
            # One unit is one cent of marked-up usage.
            "unit_amount": 1,
            "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id},
            "lookup_key": lookup_key,
            "transfer_lookup_key": True,
        }
    )


def _ensure_portal(client):
    configurations = client.v1.billing_portal.configurations.list({"limit": 100})
    for configuration in configurations.data:
        if (configuration.metadata or {}).get("wakevane") == "portal_v1":
            return configuration
    return client.v1.billing_portal.configurations.create(
        {
            "business_profile": {"headline": "WakeVane seat billing"},
            "features": {
                "customer_update": {"enabled": True, "allowed_updates": ["email", "address"]},
                "invoice_history": {"enabled": True},
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            },
            "metadata": {"wakevane": "portal_v1"},
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pro-usd", type=float, default=29.0, help="Pro seat price per month")
    parser.add_argument("--team-usd", type=float, default=25.0, help="Team seat price per month")
    parser.add_argument("--team-min-seats", type=int, default=3)
    parser.add_argument("--included-credit-usd", type=float, default=20.0)
    parser.add_argument("--markup-percent", type=int, default=20)
    parser.add_argument("--default-overage-cap-usd", type=float, default=50.0)
    parser.add_argument("--app-url", default="https://app.wakevane.com")
    args = parser.parse_args(argv)

    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret:
        print("STRIPE_SECRET_KEY is required", file=sys.stderr)
        return 2
    try:
        import stripe
    except ImportError:
        print(
            "Install the billing extra first: uv pip install 'omnigent[billing]'", file=sys.stderr
        )
        return 2
    client = stripe.StripeClient(secret)

    product = _ensure_product(client, "WakeVane seat", "seat_v1")
    pro = _ensure_seat_price(client, product.id, "wakevane_pro_seat_v1", args.pro_usd)
    team = _ensure_seat_price(client, product.id, "wakevane_team_seat_v1", args.team_usd)
    meter = _ensure_meter(client)
    usage = _ensure_usage_price(client, product.id, meter.id)
    portal = _ensure_portal(client)

    config = {
        "plans": {
            "pro": {
                "price_seat": pro.id,
                "included_credit_usd": args.included_credit_usd,
                "max_concurrent_roots": 1,
            },
            "team": {
                "price_seat": team.id,
                "included_credit_usd": args.included_credit_usd,
                "max_concurrent_roots": 3,
                "min_seats": args.team_min_seats,
            },
        },
        "price_usage": usage.id,
        "meter_event_name": METER_EVENT_NAME,
        "markup_percent": args.markup_percent,
        "default_overage_cap_usd": args.default_overage_cap_usd,
        "grace_days": 7,
    }
    mode = "test" if secret.startswith("sk_test_") else "live"
    print(f"# Stripe {mode} mode objects ready (portal configuration {portal.id})")
    print("OMNIGENT_BILLING_CONFIG=" + json.dumps(config, separators=(",", ":")))
    print()
    print("Register a webhook endpoint for these events and copy its signing secret:")
    print(f"  URL:    {args.app_url.rstrip('/')}/v1/billing/webhooks/stripe")
    print("  Events: " + ", ".join(WEBHOOK_EVENTS))
    print("  -> OMNIGENT_BILLING_STRIPE_WEBHOOK_SECRET=whsec_...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
