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
        self.failure: type[httpx.HTTPError] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure:
            raise self.failure("synthetic provider failure", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error"})
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            usage = {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 600},
            }
            message = {
                "role": "assistant",
                "content": None,
                "reasoning_content": "synthetic reasoning",
                "tool_calls": [
                    {
                        "id": "call-test",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"test.py"}'},
                    }
                ],
            }
            if not body.get("stream"):
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": message, "finish_reason": "tool_calls"}],
                        "usage": usage,
                    },
                )
            events = [
                {"choices": [{"delta": message, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": usage},
            ]
            data = b"".join(f"data: {json.dumps(event)}\r\n\r\n".encode() for event in events)
            if not self.truncate_stream:
                data += b"data: [DONE]\r\n\r\n"

            async def chat_stream() -> AsyncIterator[bytes]:
                for offset in range(0, len(data), 7):
                    yield data[offset : offset + 7]

            return httpx.Response(
                200, content=chat_stream(), headers={"content-type": "text/event-stream"}
            )
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
    from omnigent.onboarding.provider_config import provider_credential_env_vars
    from omnigent.onboarding.sandboxes.base import split_launch_env
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
    launch_config = launched.deployment.default.host_config
    # The token rides launch-only env; the config file keeps the env reference,
    # which the host forwards to its runners.
    on_disk, launch_env = split_launch_env(launch_config)
    assert on_disk == catalog_config
    assert sorted(provider_credential_env_vars(on_disk)) == [
        "OMNIGENT_WAKEVANE_TRIAL_GATEWAY_TOKEN",
        "WAKEVANE_TRIAL_GATEWAY_TOKEN",
    ]
    token = launch_env["WAKEVANE_TRIAL_GATEWAY_TOKEN"]
    assert token not in json.dumps(on_disk)
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


_CHAT_MODEL = "qwen-test"
_CHAT_GATEWAY = "/v1/funded-trial/gateway/openai/v1/chat/completions"
_CHAT_CONFIG = {
    "chat_providers": {
        "hosted": {
            "base_url": "https://chat.test/v1",
            "api_key_env": "CHAT_TEST_KEY",
            "display_name": "Hosted test",
        },
    },
    "models": {
        _CHAT_MODEL: {
            "provider": "hosted",
            "upstream_model": "vendor/qwen-test",
            "upstream_provider": "synthetic-endpoint",
            "display_name": "Qwen test",
            "input": 2,
            "output": 4,
            "cache_read": 0.5,
            "cache_write": 2,
        },
        "deepseek-test": {
            "provider": "hosted",
            "input": 2,
            "output": 4,
            "cache_read": 0.5,
            "cache_write": 2,
        },
    },
    "max_output_tokens": 1000,
}


@pytest.fixture
def chat_trial(trial: dict[str, Any]) -> dict[str, Any]:
    policy = trial["policy"]
    policy.config = FundedTrialConfig.from_mapping(_CHAT_CONFIG)
    policy._environ = {**_ENVIRON, "CHAT_TEST_KEY": "chat-service-synthetic"}
    return trial


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_gateway_tools_cache_billing_and_credentials(chat_trial, stream):
    client, policy = chat_trial["client"], chat_trial["policy"]
    await _grant(client)
    token = _token(policy, models=(_CHAT_MODEL,))
    response = await client.post(
        _CHAT_GATEWAY,
        headers={"authorization": f"Bearer {token}"},
        json=_message(
            stream=stream,
            model=_CHAT_MODEL,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
        ),
    )
    assert response.status_code == 200
    assert b"call-test" in response.content and b"reasoning_content" in response.content
    forwarded = chat_trial["upstream"].requests[0]
    assert str(forwarded.url) == "https://chat.test/v1/chat/completions"
    assert forwarded.headers["authorization"] == "Bearer chat-service-synthetic"
    assert "x-api-key" not in forwarded.headers
    assert token not in str(forwarded.headers)
    body = json.loads(forwarded.content)
    assert body["model"] == "vendor/qwen-test"
    assert body["provider"] == {
        "only": ["synthetic-endpoint"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    if stream:
        assert body["stream_options"] == {"include_usage": True}
    grant = chat_trial["store"].get_grant("owner")
    assert (grant.reserved_micros, grant.spent_micros) == (0, 1900)
    # A subsequent tool result and reasoning must reach the provider unchanged.
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "synthetic reasoning",
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-test", "content": "synthetic file"},
    ]
    followup = await client.post(
        _CHAT_GATEWAY,
        headers={"authorization": f"Bearer {token}"},
        json=_message(model=_CHAT_MODEL, messages=messages),
    )
    assert followup.status_code == 200
    assert json.loads(chat_trial["upstream"].requests[-1].content)["messages"] == messages


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "deepseek-test"},
        {"n": 2},
        {"n": True},
        {"models": ["expensive"]},
        {"provider": {"order": ["other"]}},
        {"plugins": [{"id": "web"}]},
        {"tools": [{"type": "web_search"}]},
        {"tools": 1},
        {"stream": "true"},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "https://image.test"}}],
                }
            ]
        },
    ],
)
async def test_chat_rejects_unpriced_or_unscoped_requests(chat_trial, patch):
    await _grant(chat_trial["client"])
    response = await chat_trial["client"].post(
        _CHAT_GATEWAY,
        headers={"authorization": f"Bearer {_token(chat_trial['policy'], models=(_CHAT_MODEL,))}"},
        json=_message(model=_CHAT_MODEL) | patch,
    )
    assert response.status_code in {400, 403}
    assert chat_trial["upstream"].requests == []
    assert chat_trial["store"].get_grant("owner").spent_micros == 0


async def test_chat_truncated_usage_retains_hold(chat_trial):
    await _grant(chat_trial["client"])
    chat_trial["upstream"].truncate_stream = True
    response = await chat_trial["client"].post(
        _CHAT_GATEWAY,
        headers={"authorization": f"Bearer {_token(chat_trial['policy'], models=(_CHAT_MODEL,))}"},
        json=_message(model=_CHAT_MODEL, stream=True, max_tokens=99999),
    )
    assert response.status_code == 200
    raw = chat_trial["upstream"].requests[0].content
    assert json.loads(raw)["max_tokens"] == 1000
    grant = chat_trial["store"].get_grant("owner")
    assert grant.reserved_micros == 0
    assert grant.spent_micros == max(
        1900, chat_trial["policy"].config.inference_hold_micros(_CHAT_MODEL, len(raw), 1000)
    )


async def test_chat_catalog_is_scoped_and_protocols_cannot_cross(chat_trial):
    await _grant(chat_trial["client"])
    headers = {"authorization": f"Bearer {_token(chat_trial['policy'], models=(_CHAT_MODEL,))}"}
    listing = await chat_trial["client"].get(
        "/v1/funded-trial/gateway/openai/v1/models", headers=headers
    )
    assert [row["id"] for row in listing.json()["data"]] == [_CHAT_MODEL]
    response = await chat_trial["client"].post(
        _GATEWAY, headers=headers, json=_message(model=_CHAT_MODEL)
    )
    assert response.status_code == 403
    assert chat_trial["upstream"].requests == []


async def test_chat_missing_credentials_and_provider_rejection_do_not_bill(chat_trial):
    await _grant(chat_trial["client"])
    headers = {"authorization": f"Bearer {_token(chat_trial['policy'], models=(_CHAT_MODEL,))}"}
    chat_trial["policy"]._environ = _ENVIRON
    response = await chat_trial["client"].post(
        _CHAT_GATEWAY, headers=headers, json=_message(model=_CHAT_MODEL)
    )
    assert response.status_code == 503
    assert not chat_trial["upstream"].requests
    chat_trial["policy"]._environ = {**_ENVIRON, "CHAT_TEST_KEY": "synthetic"}
    chat_trial["upstream"].status = 429
    response = await chat_trial["client"].post(
        _CHAT_GATEWAY, headers=headers, json=_message(model=_CHAT_MODEL)
    )
    assert response.status_code == 429
    grant = chat_trial["store"].get_grant("owner")
    assert (grant.spent_micros, grant.reserved_micros) == (0, 0)


async def test_hosted_catalog_and_snapshot_preserve_selection_and_prices(chat_trial):
    from types import SimpleNamespace

    from omnigent.server.funded_trial import CHAT_PROVIDER, bind_trial_inference
    from omnigent.server.inference_catalog import SandboxInferenceService
    from omnigent.server.managed_hosts import _parse_host_config

    deployment = _trial_deployment(chat_trial["policy"])
    bind_trial_inference(deployment, chat_trial["policy"])
    config = deployment.default.host_config
    assert _parse_host_config({"host_config": config}) == config
    state = SimpleNamespace(
        sandbox_config=deployment, databricks_store=None, databricks_client=None
    )
    service = SandboxInferenceService(state)
    snapshot = await service.prepare(None, "pi-native", "owner")
    assert snapshot["catalog"]["status"] == "ready"
    rows = snapshot["catalog"]["models"]
    selected = next(row for row in rows if row["id"] == _CHAT_MODEL)
    assert selected["providerLabel"] == "Hosted test"
    assert selected["pricing"] == {"input": 2, "output": 4, "cacheRead": 0.5, "cacheWrite": 2}
    assert selected["displayName"] == "Qwen test"
    assert "chat-service-synthetic" not in json.dumps(snapshot)
    assert "chat.test" not in json.dumps(snapshot)
    assert snapshot["runtime_config"]["providers"][CHAT_PROVIDER]["openai"]["wire_api"] == "chat"
    # A session's saved profile keeps its catalog through later deployment changes.
    deployment.default.host_config["providers"][CHAT_PROVIDER]["model_catalog"].clear()
    restored = await service.catalog(json.loads(json.dumps(snapshot)))
    assert restored["models"] == rows


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 80},
            {"input_tokens": 20, "output_tokens": 20, "cache_read_input_tokens": 80},
        ),
        (
            {"prompt_tokens": 0, "completion_tokens": 0},
            {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
        ),
        ({"prompt_tokens": 100}, {}),
        ({"prompt_tokens": 100, "completion_tokens": True}, {}),
        ({"prompt_tokens": 100, "completion_tokens": 1, "prompt_cache_hit_tokens": 101}, {}),
    ],
)
async def test_chat_usage_validation(usage, expected):
    from omnigent.server.routes.funded_trial import _chat_usage

    assert _chat_usage(usage) == expected


async def test_chat_done_without_usage_or_with_error_is_incomplete():
    from omnigent.server.routes.funded_trial import _StreamUsage

    for data in [
        b"data: [DONE]\n\n",
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
        b'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n',
    ]:
        parser = _StreamUsage(chat=True)
        parser.feed(data)
        assert not parser.completed


@pytest.mark.parametrize(
    "field,value",
    [
        ("input", -1),
        ("output", float("nan")),
        ("cache_read", float("inf")),
        ("provider", "unknown"),
        ("upstream_model", "model:online"),
        ("upstream_provider", ""),
    ],
)
async def test_chat_config_rejects_unpriced_routes(field, value):
    import copy

    config = copy.deepcopy(_CHAT_CONFIG)
    config["models"][_CHAT_MODEL][field] = value
    with pytest.raises(ValueError):
        FundedTrialConfig.from_mapping(config)


@pytest.mark.parametrize(
    "failure,charged", [(httpx.ConnectError, False), (httpx.ReadTimeout, True)]
)
async def test_chat_transport_failures_distinguish_unknown_usage(chat_trial, failure, charged):
    await _grant(chat_trial["client"])
    chat_trial["upstream"].failure = failure
    response = await chat_trial["client"].post(
        _CHAT_GATEWAY,
        headers={"authorization": f"Bearer {_token(chat_trial['policy'], models=(_CHAT_MODEL,))}"},
        json=_message(model=_CHAT_MODEL),
    )
    assert response.status_code == 502
    grant = chat_trial["store"].get_grant("owner")
    assert grant.reserved_micros == 0
    assert (grant.spent_micros > 0) is charged
