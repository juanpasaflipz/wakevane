"""Funded trial routes: caller summary, operator grants and the metered gateway.

The gateway proxies Anthropic Messages requests from funded workspaces. Each
request presents a short-lived scoped token, reserves its maximum cost in the
shared ledger before dispatch, and settles the provider-reported usage after.
The service API key is attached here and never reaches the workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.funded_trial import FundedTrialPolicy, GatewayClaims
from omnigent.server.funded_trial_store import (
    CAPABILITIES,
    MICROS_PER_USD,
    FundedTrialDenied,
    TrialCharge,
)
from omnigent.server.routes._auth_helpers import require_admin, require_user

_logger = logging.getLogger(__name__)

# Server-executed tools bill outside token usage, so the gateway cannot price them.
_UNPRICED_TOOL_PREFIXES = ("web_search", "web_fetch", "code_execution", "mcp")
_UNPRICED_REQUEST_FIELDS = ("mcp_servers", "container")
_FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta")
_DENIAL_STATUS = {
    "disabled": 403,
    "no_grant": 403,
    "revoked": 403,
    "expired": 403,
    "capability": 403,
    "invalid_token": 401,
    "token_expired": 401,
    "allowance_exhausted": 402,
    "concurrency": 429,
    "unconfigured": 503,
}


class GrantRequest(BaseModel):
    """Operator-chosen terms for one account's trial."""

    allowance_usd: float = Field(gt=0, le=10_000)
    expires_in_days: float = Field(gt=0, le=366)
    capabilities: list[str] = Field(min_length=1)
    max_concurrent_roots: int = Field(default=1, ge=1, le=10)


def _anthropic_error(status: int, error_type: str, message: str) -> JSONResponse:
    """Anthropic-shaped error so harness clients surface the reason verbatim."""
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _denied(denied: FundedTrialDenied) -> JSONResponse:
    status = _DENIAL_STATUS.get(denied.reason, 403)
    error_type = "authentication_error" if status == 401 else "permission_error"
    return _anthropic_error(status, error_type, denied.message)


def _presented_token(request: Request) -> str:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _merge_usage(total: dict[str, int], usage: Any) -> None:
    """Fold a usage block in; streamed counts are cumulative, so keep maxima."""
    if not isinstance(usage, dict):
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            total[key] = max(total.get(key, 0), value)


class _StreamUsage:
    """Incrementally parse Anthropic SSE frames for usage and completion."""

    def __init__(self) -> None:
        self.usage: dict[str, int] = {}
        self.completed = False
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "message_start":
                _merge_usage(self.usage, (event.get("message") or {}).get("usage"))
            elif kind == "message_delta":
                _merge_usage(self.usage, event.get("usage"))
            elif kind == "message_stop":
                self.completed = True


def _validate_messages_body(
    policy: FundedTrialPolicy, claims: GatewayClaims, raw: bytes
) -> tuple[dict[str, Any], str, int]:
    """Return the parsed body, model and enforced output bound, or raise."""
    if len(raw) > policy.config.max_request_bytes:
        raise ValueError("Request exceeds the funded trial size limit.")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    model = body.get("model")
    if not isinstance(model, str) or model not in claims.models:
        raise PermissionError(f"Model {model!r} is not approved for this funded session.")
    if model not in policy.config.model_pricing:
        raise PermissionError(f"Model {model!r} has no approved price.")
    max_tokens = body.get("max_tokens")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not 1 <= max_tokens <= policy.config.max_output_tokens
    ):
        raise ValueError(
            f"max_tokens must be between 1 and {policy.config.max_output_tokens} "
            "for funded requests."
        )
    for field_name in _UNPRICED_REQUEST_FIELDS:
        if field_name in body:
            raise PermissionError(f"{field_name} is not available for funded requests.")
    for tool in body.get("tools") or []:
        tool_type = tool.get("type") if isinstance(tool, dict) else None
        if isinstance(tool_type, str) and tool_type.startswith(_UNPRICED_TOOL_PREFIXES):
            raise PermissionError(f"Tool {tool_type!r} is not available for funded requests.")
    return body, model, max_tokens


def create_funded_trial_router(
    *,
    policy: FundedTrialPolicy,
    auth_provider: AuthProvider | None,
    permission_store: Any,
    transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    """Build the trial routes around one shared policy."""
    router = APIRouter()

    async def _operator(request: Request, action: str) -> str:
        if auth_provider is None:
            raise OmnigentError(
                "Funded trials require multi-user authentication", code=ErrorCode.FORBIDDEN
            )
        actor = await require_admin(request, auth_provider, permission_store, action=action)
        assert actor is not None
        if policy.store is None:
            raise OmnigentError(
                "Funded trial storage is unavailable", code=ErrorCode.INTERNAL_ERROR
            )
        return actor

    @router.get("/funded-trial")
    async def my_trial(request: Request, response: Response) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        response.headers["Cache-Control"] = "private, no-store"
        return await asyncio.to_thread(policy.summary, user_id)

    @router.put("/admin/funded-trial/grants/{user_id}")
    async def put_grant(request: Request, user_id: str, body: GrantRequest) -> dict[str, Any]:
        actor = await _operator(request, "grant a funded trial")
        unknown = set(body.capabilities) - CAPABILITIES
        if unknown:
            raise OmnigentError(
                f"Unknown capabilities: {sorted(unknown)}", code=ErrorCode.INVALID_INPUT
            )
        if permission_store is not None and not await asyncio.to_thread(
            permission_store.get_user, user_id
        ):
            raise OmnigentError("No account has this user id", code=ErrorCode.NOT_FOUND)
        assert policy.store is not None
        grant = await asyncio.to_thread(
            policy.store.upsert_grant,
            user_id,
            allowance_micros=round(body.allowance_usd * MICROS_PER_USD),
            expires_at=int(time.time() + body.expires_in_days * 86_400),
            capabilities=body.capabilities,
            max_concurrent_roots=body.max_concurrent_roots,
            actor_id=actor,
        )
        return {"user_id": grant.user_id, "summary": policy.summary(grant.user_id)}

    @router.delete("/admin/funded-trial/grants/{user_id}")
    async def revoke_grant(request: Request, user_id: str) -> dict[str, Any]:
        actor = await _operator(request, "revoke a funded trial")
        assert policy.store is not None
        grant = await asyncio.to_thread(policy.store.revoke_grant, user_id, actor_id=actor)
        if grant is None:
            raise OmnigentError("No funded trial for this account", code=ErrorCode.NOT_FOUND)
        return {"user_id": user_id, "revoked_at": grant.revoked_at}

    @router.get("/admin/funded-trial/grants/{user_id}")
    async def get_grant(request: Request, user_id: str) -> dict[str, Any]:
        await _operator(request, "read a funded trial")
        assert policy.store is not None
        store = policy.store
        grant, charges, events = await asyncio.gather(
            asyncio.to_thread(store.get_grant, user_id),
            asyncio.to_thread(store.open_charges, user_id),
            asyncio.to_thread(store.list_events, user_id),
        )
        if grant is None:
            raise OmnigentError("No funded trial for this account", code=ErrorCode.NOT_FOUND)
        return {
            "grant": {
                "id": grant.id,
                "capabilities": sorted(grant.capabilities),
                "allowance_micros": grant.allowance_micros,
                "reserved_micros": grant.reserved_micros,
                "spent_micros": grant.spent_micros,
                "expires_at": grant.expires_at,
                "revoked_at": grant.revoked_at,
                "max_concurrent_roots": grant.max_concurrent_roots,
            },
            "open_charges": [charge.__dict__ for charge in charges],
            "events": events,
        }

    async def _authorize(request: Request) -> GatewayClaims:
        return await asyncio.to_thread(policy.verify_gateway_token, _presented_token(request))

    def _upstream_headers(request: Request) -> dict[str, str]:
        headers = {
            name: value
            for name in _FORWARDED_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        headers.setdefault("anthropic-version", "2023-06-01")
        headers["content-type"] = "application/json"
        # Usage is parsed from relayed bytes, so they must arrive uncompressed.
        headers["accept-encoding"] = "identity"
        headers["x-api-key"] = policy.upstream_api_key()
        return headers

    @router.post("/funded-trial/gateway/anthropic/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        try:
            claims = await _authorize(request)
            raw = await request.body()
            _validate_count_body(claims, raw)
            headers = _upstream_headers(request)
        except FundedTrialDenied as denied:
            return _denied(denied)
        except PermissionError as exc:
            return _anthropic_error(403, "permission_error", str(exc))
        except ValueError as exc:
            return _anthropic_error(400, "invalid_request_error", str(exc))
        async with httpx.AsyncClient(transport=transport, timeout=60) as client:
            upstream = await client.post(
                f"{policy.config.upstream_base_url}/v1/messages/count_tokens",
                content=raw,
                headers=headers,
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type="application/json",
        )

    @router.post("/funded-trial/gateway/anthropic/v1/messages")
    async def messages(request: Request) -> Response:
        store = policy.store
        try:
            claims = await _authorize(request)
            raw = await request.body()
            body, model, max_tokens = _validate_messages_body(policy, claims, raw)
            headers = _upstream_headers(request)
        except FundedTrialDenied as denied:
            return _denied(denied)
        except PermissionError as exc:
            return _anthropic_error(403, "permission_error", str(exc))
        except ValueError as exc:
            return _anthropic_error(400, "invalid_request_error", str(exc))
        assert store is not None
        hold = policy.config.inference_hold_micros(model, len(raw), max_tokens)
        try:
            charge = await asyncio.to_thread(
                store.reserve,
                claims.user_id,
                capability="inference",
                kind="inference",
                amount_micros=hold,
                idempotency_key=f"gateway:{uuid.uuid4().hex}",
                root_session_id=claims.root_session_id,
                session_id=claims.session_id,
                detail={"model": model, "max_tokens": max_tokens, "request_bytes": len(raw)},
            )
        except FundedTrialDenied as denied:
            return _denied(denied)
        url = f"{policy.config.upstream_base_url}/v1/messages"
        client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600, connect=30))
        try:
            upstream_request = client.build_request("POST", url, content=raw, headers=headers)
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            # Nothing reached the provider, so the hold is safe to return.
            await asyncio.to_thread(store.release, charge.id)
            return _anthropic_error(502, "api_error", "The model provider is unreachable.")
        if upstream.status_code >= 400:
            content = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            # The provider does not bill rejected requests.
            await asyncio.to_thread(store.release, charge.id)
            return Response(
                content=content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )
        if body.get("stream"):
            return StreamingResponse(
                _relay_stream(policy, charge, model, upstream, client),
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "text/event-stream"),
            )
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        usage: dict[str, int] = {}
        try:
            _merge_usage(usage, json.loads(content).get("usage"))
        except (ValueError, AttributeError):
            usage = {}
        await _settle(policy, charge, model, usage, complete=bool(usage))
        return Response(
            content=content, status_code=upstream.status_code, media_type="application/json"
        )

    return router


def _validate_count_body(claims: GatewayClaims, raw: bytes) -> None:
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("model") not in claims.models:
        raise PermissionError("Model is not approved for this funded session.")


async def _settle(
    policy: FundedTrialPolicy,
    charge: TrialCharge,
    model: str,
    usage: dict[str, int],
    *,
    complete: bool,
) -> None:
    """Settle measured usage; an unknown outcome keeps the whole hold as spend."""
    assert policy.store is not None
    measured = policy.config.inference_cost_micros(model, usage) if usage else 0
    amount = measured if complete else max(measured, charge.reserved_micros)
    detail = {"model": model, "usage": usage, "usage_complete": complete}
    try:
        await asyncio.to_thread(policy.store.settle, charge.id, amount, detail=detail)
    except Exception:  # an unsettled hold stays reserved and still blocks overspend
        _logger.exception("Funded trial settlement failed for charge %s", charge.id)


async def _relay_stream(
    policy: FundedTrialPolicy,
    charge: TrialCharge,
    model: str,
    upstream: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    parser = _StreamUsage()
    try:
        async for chunk in upstream.aiter_raw():
            parser.feed(chunk)
            yield chunk
    finally:
        # Shielded so a client disconnect cannot cancel settlement midway.
        settlement = asyncio.ensure_future(
            _close_and_settle(policy, charge, model, upstream, client, parser)
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(settlement)


async def _close_and_settle(
    policy: FundedTrialPolicy,
    charge: TrialCharge,
    model: str,
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    parser: _StreamUsage,
) -> None:
    try:
        await upstream.aclose()
        await client.aclose()
    finally:
        await _settle(policy, charge, model, parser.usage, complete=parser.completed)
