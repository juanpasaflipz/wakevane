"""Seat billing routes: caller summary, Checkout, portal, overage cap and the Stripe webhook.

All routes mount only when the ``billing`` release feature is on. The
webhook is unauthenticated by design and trusts nothing but the verified
Stripe signature; every other route acts for the signed-in caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.billing import BillingError, BillingPolicy
from omnigent.server.routes._auth_helpers import require_admin, require_user

_logger = logging.getLogger(__name__)

_ERROR_CODES = {
    "disabled": ErrorCode.FORBIDDEN,
    "unconfigured": ErrorCode.INTERNAL_ERROR,
    "unknown_plan": ErrorCode.INVALID_INPUT,
    "invalid_cap": ErrorCode.INVALID_INPUT,
    "already_subscribed": ErrorCode.CONFLICT,
    "no_customer": ErrorCode.NOT_FOUND,
    "subscription_required": ErrorCode.PAYMENT_REQUIRED,
    "malformed_event": ErrorCode.INVALID_INPUT,
}


class CheckoutRequest(BaseModel):
    """Body of ``POST /v1/billing/checkout``."""

    plan: str | None = Field(default=None, max_length=32)
    seats: int | None = Field(default=None, ge=1, le=500)


class OverageRequest(BaseModel):
    """Body of ``PUT /v1/billing/overage``."""

    enabled: bool
    cap_usd: float | None = Field(default=None, ge=0, le=100_000)


def _error(error: BillingError) -> OmnigentError:
    return OmnigentError(error.message, code=_ERROR_CODES.get(error.reason, ErrorCode.FORBIDDEN))


def create_billing_router(
    *,
    policy: BillingPolicy,
    auth_provider: AuthProvider | None,
    permission_store: Any,
) -> APIRouter:
    """Build the billing routes around one shared policy."""
    router = APIRouter()

    def _caller(request: Request) -> str:
        if auth_provider is None:
            raise OmnigentError(
                "Seat billing requires multi-user authentication", code=ErrorCode.FORBIDDEN
            )
        user_id = require_user(request, auth_provider)
        assert user_id is not None
        return user_id

    @router.get("/billing")
    async def my_billing(request: Request, response: Response) -> dict[str, Any]:
        user_id = _caller(request)
        response.headers["Cache-Control"] = "private, no-store"
        return await asyncio.to_thread(policy.summary, user_id)

    @router.post("/billing/checkout")
    async def checkout(request: Request, body: CheckoutRequest) -> dict[str, Any]:
        user_id = _caller(request)
        try:
            url = await asyncio.to_thread(
                policy.checkout_url, user_id, plan_key=body.plan, seats=body.seats
            )
        except BillingError as error:
            raise _error(error) from None
        return {"url": url}

    @router.post("/billing/portal")
    async def portal(request: Request) -> dict[str, Any]:
        user_id = _caller(request)
        try:
            url = await asyncio.to_thread(policy.portal_url, user_id)
        except BillingError as error:
            raise _error(error) from None
        return {"url": url}

    @router.put("/billing/overage")
    async def overage(request: Request, body: OverageRequest) -> dict[str, Any]:
        user_id = _caller(request)
        try:
            await asyncio.to_thread(
                policy.set_overage, user_id, enabled=body.enabled, cap_usd=body.cap_usd
            )
        except BillingError as error:
            raise _error(error) from None
        return await asyncio.to_thread(policy.summary, user_id)

    @router.post("/billing/webhooks/stripe")
    async def stripe_webhook(request: Request) -> Response:
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            result = await asyncio.to_thread(policy.handle_webhook, payload, signature)
        except BillingError as error:
            status = 503 if error.reason in {"disabled", "unconfigured"} else 400
            return JSONResponse(status_code=status, content={"error": error.message})
        except Exception as exc:  # noqa: BLE001 - any failure must be a non-2xx for Stripe
            # A failed signature check or a provider error must not look like success,
            # or Stripe would stop redelivering the event.
            _logger.warning("Stripe webhook rejected: %s", exc)
            return JSONResponse(status_code=400, content={"error": "webhook rejected"})
        return JSONResponse(status_code=200, content=result)

    @router.get("/admin/billing/accounts/{user_id}")
    async def admin_account(request: Request, user_id: str) -> dict[str, Any]:
        if auth_provider is None:
            raise OmnigentError(
                "Seat billing requires multi-user authentication", code=ErrorCode.FORBIDDEN
            )
        await require_admin(request, auth_provider, permission_store, action="read billing")
        summary = await asyncio.to_thread(policy.summary, user_id)
        account = await asyncio.to_thread(policy.account, user_id)
        if account is None:
            raise OmnigentError("No billing account for this user", code=ErrorCode.NOT_FOUND)
        return {"account": account.__dict__, "summary": summary}

    return router
