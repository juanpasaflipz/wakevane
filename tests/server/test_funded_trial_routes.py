"""Funded trial gate: operator grants, caller summary and the metered gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.funded_trial import FundedTrialConfig, FundedTrialPolicy
from omnigent.server.funded_trial_store import MICROS_PER_USD, FundedTrialDenied, FundedTrialStore
from omnigent.server.routes.funded_trial import create_funded_trial_router

pytestmark = pytest.mark.asyncio

_MODEL = "claude-sonnet-test"
_GATEWAY = "/v1/funded-trial/gateway/anthropic/v1/messages"
_SERVICE_KEY = "sk-service-synthetic"
_ENVIRON = {
    "OMNIGENT_FUNDED_TRIAL_SIGNING_KEY": "s" * 40,
    "OMNIGENT_FUNDED_TRIAL_ANTHROPIC_API_KEY": _SERVICE_KEY,
}
_CONFIG = FundedTrialConfig.from_mapping(
    {
        "models": {
            _MODEL: {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
            "claude-unlisted-test": {
                "input": 3,
                "output": 15,
                "cache_read": 0.3,
                "cache_write": 3.75,
            },
        },
        "compute_usd_per_hour": {"cloud-test": 0.5},
        "max_output_tokens": 1000,
        "upstream_base_url": "https://upstream.test",
    }
)


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


class _Accounts:
    def is_admin(self, user_id: str) -> bool:
        return user_id == "operator"

    def get_user(self, user_id: str) -> object | None:
        return object() if user_id in {"owner", "other", "operator"} else None


class _Upstream:
    """Fake Anthropic upstream recording what the gateway forwarded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.truncate_stream = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error"})
        body = json.loads(request.content)
        usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000}
        if not body.get("stream"):
            return httpx.Response(200, json={"type": "message", "usage": usage})
        events = [
            {"type": "message_start", "message": {"usage": {**usage, "output_tokens": 1}}},
            {"type": "message_delta", "usage": {"output_tokens": 200}},
            {"type": "message_stop"},
        ]
        if self.truncate_stream:
            events = events[:1]
        frames = [f"event: x\ndata: {json.dumps(event)}\n\n".encode() for event in events]

        async def stream() -> AsyncIterator[bytes]:
            for frame in frames:
                yield frame

        return httpx.Response(200, content=stream(), headers={"content-type": "text/event-stream"})


@pytest_asyncio.fixture
async def trial(db_uri: str) -> AsyncIterator[dict[str, Any]]:
    store = FundedTrialStore(db_uri)
    policy = FundedTrialPolicy(enabled=True, store=store, config=_CONFIG, environ=_ENVIRON)
    upstream = _Upstream()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.include_router(
        create_funded_trial_router(
            policy=policy,
            auth_provider=_HeaderAuth(),  # type: ignore[arg-type]
            permission_store=_Accounts(),
            transport=httpx.MockTransport(upstream.handler),
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wakevane.test"
    ) as client:
        yield {"client": client, "policy": policy, "store": store, "upstream": upstream}


async def _grant(client: httpx.AsyncClient, user: str = "owner", **terms: Any) -> httpx.Response:
    body = {
        "allowance_usd": 100,
        "expires_in_days": 30,
        "capabilities": ["inference", "compute"],
        **terms,
    }
    return await client.put(
        f"/v1/admin/funded-trial/grants/{user}", json=body, headers={"x-test-user": "operator"}
    )


def _token(policy: FundedTrialPolicy, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "root_session_id": "root-1",
        "session_id": "session-1",
        "models": (_MODEL,),
    }
    arguments.update(overrides)
    return policy.mint_gateway_token("owner", **arguments)


def _message(stream: bool = False, **overrides: Any) -> dict[str, Any]:
    return {
        "model": _MODEL,
        "max_tokens": 500,
        "stream": stream,
        "messages": [{"role": "user", "content": "hello"}],
        **overrides,
    }


async def test_only_operators_grant_and_callers_see_only_themselves(
    trial: dict[str, Any],
) -> None:
    client = trial["client"]
    denied = await client.put(
        "/v1/admin/funded-trial/grants/owner",
        json={"allowance_usd": 100, "expires_in_days": 30, "capabilities": ["inference"]},
        headers={"x-test-user": "owner"},
    )
    assert denied.status_code == 403
    assert (await _grant(client, user="missing-account")).status_code == 404
    assert (await _grant(client)).status_code == 200
    owner = (await client.get("/v1/funded-trial", headers={"x-test-user": "owner"})).json()
    other = (await client.get("/v1/funded-trial", headers={"x-test-user": "other"})).json()
    assert owner["enabled"] and owner["remaining_usd"] == 100
    assert other == {"enabled": False, "capabilities": [], "remaining_usd": 0.0}


async def test_other_account_cannot_obtain_gateway_authority(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    with pytest.raises(FundedTrialDenied) as denied:
        trial["policy"].mint_gateway_token("other", root_session_id="r", session_id="s")
    assert denied.value.reason == "no_grant"
    forged = _token(trial["policy"])[:-4] + "AAAA"
    response = await trial["client"].post(_GATEWAY, json=_message(), headers={"x-api-key": forged})
    assert response.status_code == 401
    assert trial["upstream"].requests == []


async def test_gateway_meters_streamed_usage_with_server_key(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    token = _token(trial["policy"])
    response = await trial["client"].post(
        _GATEWAY, json=_message(stream=True), headers={"x-api-key": token}
    )
    assert response.status_code == 200 and b"message_stop" in response.content
    forwarded = trial["upstream"].requests[0]
    assert forwarded.headers["x-api-key"] == _SERVICE_KEY
    assert token not in forwarded.headers.values()
    grant = trial["store"].get_grant("owner")
    # 1000 input * $3/M + 200 output * $15/M + 5000 cache reads * $0.30/M
    assert (grant.reserved_micros, grant.spent_micros) == (0, 7_500)


async def test_truncated_stream_keeps_the_full_hold(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    trial["upstream"].truncate_stream = True
    raw = json.dumps(_message(stream=True)).encode()
    await trial["client"].post(
        _GATEWAY,
        content=raw,
        headers={"x-api-key": _token(trial["policy"]), "content-type": "application/json"},
    )
    grant = trial["store"].get_grant("owner")
    assert grant.reserved_micros == 0
    assert grant.spent_micros == _CONFIG.inference_hold_micros(_MODEL, len(raw), 500)


async def test_rejected_upstream_request_releases_the_hold(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    trial["upstream"].status = 529
    response = await trial["client"].post(
        _GATEWAY, json=_message(), headers={"x-api-key": _token(trial["policy"])}
    )
    assert response.status_code == 529
    grant = trial["store"].get_grant("owner")
    assert (grant.reserved_micros, grant.spent_micros) == (0, 0)


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"model": "claude-unlisted-test"}, 403),
        ({"max_tokens": 5000}, 400),
        ({"tools": [{"type": "web_search_20250305", "name": "web_search"}]}, 403),
        ({"mcp_servers": [{"url": "https://mcp.test"}]}, 403),
    ],
)
async def test_unapproved_or_unpriced_requests_never_reach_the_provider(
    trial: dict[str, Any], overrides: dict[str, Any], status: int
) -> None:
    await _grant(trial["client"])
    response = await trial["client"].post(
        _GATEWAY, json=_message(**overrides), headers={"x-api-key": _token(trial["policy"])}
    )
    assert response.status_code == status
    assert trial["upstream"].requests == []
    assert trial["store"].get_grant("owner").reserved_micros == 0


async def test_exhausted_allowance_blocks_dispatch(trial: dict[str, Any]) -> None:
    await _grant(trial["client"], allowance_usd=0.001)
    response = await trial["client"].post(
        _GATEWAY, json=_message(), headers={"x-api-key": _token(trial["policy"])}
    )
    assert response.status_code == 402
    assert trial["upstream"].requests == []


async def test_revocation_retires_issued_tokens_even_after_regrant(
    trial: dict[str, Any],
) -> None:
    client = trial["client"]
    await _grant(client)
    token = _token(trial["policy"])
    revoked = await client.delete(
        "/v1/admin/funded-trial/grants/owner", headers={"x-test-user": "operator"}
    )
    assert revoked.status_code == 200
    response = await client.post(_GATEWAY, json=_message(), headers={"x-api-key": token})
    assert response.status_code == 403
    await _grant(client)
    response = await client.post(_GATEWAY, json=_message(), headers={"x-api-key": token})
    assert response.status_code == 401
    assert trial["upstream"].requests == []


async def test_kill_switch_denies_everything(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    token = _token(trial["policy"])
    trial["policy"].enabled = False
    response = await trial["client"].post(_GATEWAY, json=_message(), headers={"x-api-key": token})
    assert response.status_code == 403
    with pytest.raises(FundedTrialDenied):
        trial["policy"].reserve_compute(
            "owner",
            provider="cloud-test",
            idempotency_key="launch-1",
            root_session_id="root-1",
            session_id="root-1",
        )
    assert trial["upstream"].requests == []


async def test_compute_requires_an_approved_price(trial: dict[str, Any]) -> None:
    await _grant(trial["client"])
    with pytest.raises(FundedTrialDenied) as denied:
        trial["policy"].reserve_compute(
            "owner",
            provider="unpriced-cloud",
            idempotency_key="launch-1",
            root_session_id="root-1",
            session_id="root-1",
        )
    assert denied.value.reason == "unpriced_compute"
    charge = trial["policy"].reserve_compute(
        "owner",
        provider="cloud-test",
        idempotency_key="launch-2",
        root_session_id="root-1",
        session_id="root-1",
    )
    assert charge.reserved_micros == 2 * MICROS_PER_USD  # 4h default * $0.50


def _trial_deployment(policy: FundedTrialPolicy | None, provider: str = "cloud-test") -> Any:
    from omnigent.server.managed_hosts import parse_sandbox_config

    deployment = parse_sandbox_config(
        {"provider": "modal", "server_url": "https://s.example.com", "funding": "trial"}
    )
    assert deployment is not None
    target = deployment.default
    target.provider = provider
    deployment.funded_trial = policy
    return deployment


async def test_operator_funded_targets_need_no_trial(trial: dict[str, Any]) -> None:
    from omnigent.server.funded_trial import admit_managed_launch
    from omnigent.server.managed_hosts import parse_sandbox_config

    deployment = parse_sandbox_config({"provider": "modal", "server_url": "https://s.example.com"})
    assert deployment is not None and deployment.default.funding == "deployment"
    assert (
        admit_managed_launch(
            deployment,
            provider=None,
            owner="other",
            session_id="s",
            root_session_id="s",
            phase="launch",
            launch_id="1",
        )
        is None
    )
    with pytest.raises(ValueError, match=r"sandbox\.funding"):
        parse_sandbox_config(
            {"provider": "modal", "server_url": "https://s.example.com", "funding": "free"}
        )
    # Trial workspaces may not carry the deployment's own provider credentials.
    with pytest.raises(ValueError, match="must not configure inference providers"):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "funding": "trial",
                "host_config": {
                    "providers": {
                        "service": {
                            "kind": "key",
                            "anthropic": {
                                "base_url": "https://api.anthropic.com",
                                "api_key_ref": "env:KEY",
                            },
                        }
                    }
                },
            }
        )


async def test_trial_target_launch_admission(trial: dict[str, Any]) -> None:
    from omnigent.server.funded_trial import admit_managed_launch, require_launch_eligibility

    await _grant(trial["client"])
    deployment = _trial_deployment(trial["policy"])

    def launch(owner: str, root: str, launch_id: str) -> Any:
        return admit_managed_launch(
            deployment,
            provider=None,
            owner=owner,
            session_id=f"{root}-child",
            root_session_id=root,
            phase="launch",
            launch_id=launch_id,
        )

    require_launch_eligibility(deployment, None, "owner")
    with pytest.raises(FundedTrialDenied):
        require_launch_eligibility(deployment, None, "other")
    with pytest.raises(FundedTrialDenied):
        launch("other", "root-x", "1")
    assert launch("owner", "root-1", "1").reserved_micros == 2 * MICROS_PER_USD
    # A child of the same root shares the slot; a second root is refused.
    assert launch("owner", "root-1", "2").status == "reserved"
    with pytest.raises(FundedTrialDenied) as denied:
        launch("owner", "root-2", "3")
    assert denied.value.reason == "concurrency"
    # Without a wired policy a trial target fails closed.
    with pytest.raises(FundedTrialDenied) as unwired:
        admit_managed_launch(
            _trial_deployment(None),
            provider=None,
            owner="owner",
            session_id="s",
            root_session_id="s",
            phase="launch",
            launch_id="4",
        )
    assert unwired.value.reason == "disabled"


async def test_trial_launch_arms_a_scoped_gateway_credential(trial: dict[str, Any]) -> None:
    from omnigent.inference_config import parse_inference_config
    from omnigent.server.funded_trial import (
        TRIAL_PROVIDER,
        bind_trial_inference,
        prepare_trial_launch,
    )
    from omnigent.server.managed_hosts import _parse_host_config

    await _grant(trial["client"])
    deployment = _trial_deployment(trial["policy"])
    bind_trial_inference(deployment, trial["policy"])
    catalog_config = deployment.default.host_config
    # The catalog/snapshot profile is a valid host config holding no credential.
    assert _parse_host_config({"host_config": catalog_config}) == catalog_config
    binding = parse_inference_config(catalog_config)["claude-sdk"]
    assert binding.provider == TRIAL_PROVIDER
    assert binding.model_allowlist == tuple(sorted(_CONFIG.model_pricing))
    family = catalog_config["providers"][TRIAL_PROVIDER]["anthropic"]
    assert family["base_url"] == "https://s.example.com/v1/funded-trial/gateway/anthropic"
    assert "api_key" not in family

    launched = prepare_trial_launch(
        deployment,
        provider=None,
        owner="owner",
        session_id="root-1-child",
        root_session_id="root-1",
        phase="launch",
        launch_id="1",
    )
    assert launched.charge is not None and launched.charge.status == "reserved"
    token = launched.deployment.default.host_config["providers"][TRIAL_PROVIDER]["anthropic"][
        "api_key"
    ]
    assert deployment.default.host_config == catalog_config  # shared config is untouched
    claims = trial["policy"].verify_gateway_token(token)
    assert (claims.user_id, claims.root_session_id) == ("owner", "root-1")
    response = await trial["client"].post(_GATEWAY, json=_message(), headers={"x-api-key": token})
    assert response.status_code == 200
    assert trial["store"].get_grant("owner").spent_micros > 0


async def test_trial_launch_without_inference_releases_its_compute_hold(
    trial: dict[str, Any],
) -> None:
    from omnigent.server.funded_trial import bind_trial_inference, prepare_trial_launch
    from omnigent.server.managed_hosts import parse_sandbox_config

    await _grant(trial["client"], capabilities=["compute"])
    deployment = _trial_deployment(trial["policy"])
    bind_trial_inference(deployment, trial["policy"])
    with pytest.raises(FundedTrialDenied) as denied:
        prepare_trial_launch(
            deployment,
            provider=None,
            owner="owner",
            session_id="root-1",
            root_session_id="root-1",
            phase="launch",
            launch_id="1",
        )
    assert denied.value.reason == "capability"
    assert trial["store"].open_charges("owner") == []
    # Operator-funded deployments launch unchanged, without any trial state.
    operator = parse_sandbox_config({"provider": "modal", "server_url": "https://s.example.com"})
    assert operator is not None
    unchanged = prepare_trial_launch(
        operator,
        provider=None,
        owner="other",
        session_id="s",
        root_session_id="s",
        phase="launch",
        launch_id="2",
    )
    assert unchanged.deployment is operator and unchanged.charge is None
