"""A user's own OpenAI key: storage, routes, session profile and launch environment."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.connections.provider_keys import OpenAIKeyStore, UserProviderKey
from omnigent.errors import OmnigentError
from omnigent.models.model_fallbacks import CODEX_DEFAULT_MODEL
from omnigent.onboarding.sandboxes.base import HOST_LAUNCH_ENV_KEY, split_launch_env
from omnigent.server.inference_catalog import SandboxInferenceService
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes.provider_keys import create_provider_keys_router
from omnigent.server.user_provider_keys import (
    USER_OPENAI_KEY_ENV,
    USER_OPENAI_PROVIDER,
    bind_user_openai_key,
    with_user_provider_keys,
)
from tests.server.test_credential_store import _FakeCipher

pytestmark = pytest.mark.asyncio

_KEY = "sk-test-" + "k" * 40
_OPERATOR_MODEL = "operator-model"


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


def _operator_config(extra_harnesses: tuple[str, ...] = ()) -> dict[str, Any]:
    """An operator profile that binds only the Claude harness, like a trial target."""
    binding = {
        "provider": "operator",
        "default_model": _OPERATOR_MODEL,
        "model_allowlist": [_OPERATOR_MODEL],
    }
    return {
        "providers": {
            "operator": {
                "kind": "gateway",
                "anthropic": {
                    "base_url": "https://gateway.example/anthropic",
                    "api_key_ref": "env:OPERATOR_TOKEN",
                },
                "openai": {
                    "base_url": "https://gateway.example/openai",
                    "api_key_ref": "env:OPERATOR_TOKEN",
                },
            }
        },
        "inference": {
            "harnesses": {name: dict(binding) for name in ("claude-sdk", *extra_harnesses)}
        },
    }


def _deployment(config: dict[str, Any]) -> ManagedSandboxDeployment:
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="http://localhost:6767",
            launcher_factory=lambda: None,
            token_ttl_s=100,
            provider="cloud-test",
            host_config=config,
        )
    )


@pytest.fixture
def store(db_uri: str) -> OpenAIKeyStore:
    return OpenAIKeyStore(db_uri, _FakeCipher())


@pytest_asyncio.fixture
async def client(store: OpenAIKeyStore) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.include_router(
        create_provider_keys_router(store, auth_provider=_HeaderAuth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wakevane.test"
    ) as http:
        yield http


async def test_routes_save_show_and_remove_only_the_callers_key(
    client: httpx.AsyncClient, store: OpenAIKeyStore
) -> None:
    owner, other = {"x-test-user": "owner"}, {"x-test-user": "other"}
    assert (await client.get("/v1/provider-keys/openai", headers=owner)).json() == {
        "connected": False,
        "key_hint": None,
        "models": [],
        "updated_at": None,
    }

    saved = await client.put(
        "/v1/provider-keys/openai",
        headers=owner,
        json={"api_key": f"  {_KEY}\n", "models": ["model-a", "model-a", "model-b"]},
    )
    assert saved.status_code == 200
    # No response ever carries the key, only its last characters.
    assert _KEY not in saved.text
    assert saved.json()["key_hint"] == _KEY[-4:]
    assert saved.json()["models"] == ["model-a", "model-b"]
    shown = await client.get("/v1/provider-keys/openai", headers=owner)
    assert shown.json()["connected"] is True and _KEY not in shown.text

    assert (await client.get("/v1/provider-keys/openai", headers=other)).json()[
        "connected"
    ] is False
    # The stored key is read back whole only on the launch path.
    assert store.get("owner").api_key is None
    assert store.get("owner", with_tokens=True).api_key == _KEY

    removed = await client.delete("/v1/provider-keys/openai", headers=other)
    assert removed.status_code == 200 and store.get("owner") is not None
    await client.delete("/v1/provider-keys/openai", headers=owner)
    assert store.get("owner") is None


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "too-short"},
        {"api_key": "has a space " + "k" * 30},
        {"api_key": _KEY, "models": ["bad model id"]},
        {"api_key": _KEY, "models": [f"m{i}" for i in range(9)]},
    ],
)
async def test_routes_reject_bad_input_without_storing_it(
    client: httpx.AsyncClient, store: OpenAIKeyStore, body: dict[str, Any]
) -> None:
    response = await client.put(
        "/v1/provider-keys/openai", headers={"x-test-user": "owner"}, json=body
    )
    assert response.status_code in {400, 422}
    # A rejected key is never echoed back to the caller.
    assert body["api_key"] not in response.text
    assert store.get("owner") is None


async def test_models_change_without_resending_the_key(
    client: httpx.AsyncClient, store: OpenAIKeyStore
) -> None:
    owner = {"x-test-user": "owner"}
    missing = await client.patch(
        "/v1/provider-keys/openai", headers=owner, json={"models": ["model-a"]}
    )
    assert missing.status_code == 404 and store.get("owner") is None

    await client.put("/v1/provider-keys/openai", headers=owner, json={"api_key": _KEY})
    changed = await client.patch(
        "/v1/provider-keys/openai", headers=owner, json={"models": [" model-a "]}
    )
    assert changed.status_code == 200 and _KEY not in changed.text
    assert changed.json()["models"] == ["model-a"]
    assert changed.json()["key_hint"] == _KEY[-4:]
    assert store.get("owner", with_tokens=True).api_key == _KEY

    # An empty list goes back to the deployment default; a bad id changes nothing.
    rejected = await client.patch(
        "/v1/provider-keys/openai", headers=owner, json={"models": ["bad model id"]}
    )
    assert rejected.status_code == 422 and store.get("owner").models == ("model-a",)
    cleared = await client.patch("/v1/provider-keys/openai", headers=owner, json={"models": []})
    assert cleared.json()["models"] == []
    # The key itself is not accepted on this route.
    extra = await client.patch(
        "/v1/provider-keys/openai", headers=owner, json={"models": [], "api_key": _KEY}
    )
    assert extra.status_code == 422


async def test_routes_require_a_signed_in_account(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/provider-keys/openai")).status_code == 401


async def test_binding_fills_only_harnesses_the_operator_left_unbound() -> None:
    key = UserProviderKey("owner", None, "abcd", (), 0)

    config = _operator_config()
    assert bind_user_openai_key(config, key) == ["codex", "codex-native"]
    assert config["providers"][USER_OPENAI_PROVIDER]["openai"]["api_key_ref"] == (
        f"env:{USER_OPENAI_KEY_ENV}"
    )
    assert config["inference"]["harnesses"]["codex-native"] == {
        "provider": USER_OPENAI_PROVIDER,
        "default_model": CODEX_DEFAULT_MODEL,
        "model_allowlist": [CODEX_DEFAULT_MODEL],
    }
    assert config["inference"]["harnesses"]["claude-sdk"]["provider"] == "operator"

    partly = _operator_config(("codex",))
    assert bind_user_openai_key(partly, key) == ["codex-native"]
    assert partly["inference"]["harnesses"]["codex"]["provider"] == "operator"

    fully = _operator_config(("codex", "codex-native"))
    assert bind_user_openai_key(fully, key) == []
    assert USER_OPENAI_PROVIDER not in fully["providers"]

    restricted = _operator_config()
    bind_user_openai_key(restricted, UserProviderKey("owner", None, "abcd", ("m1", "m2"), 0))
    assert restricted["inference"]["harnesses"]["codex"]["model_allowlist"] == ["m1", "m2"]
    assert restricted["inference"]["harnesses"]["codex"]["default_model"] == "m1"


async def test_session_profile_binds_codex_for_an_owner_with_a_key(store: OpenAIKeyStore) -> None:
    store.upsert("owner", api_key=_KEY)
    deployment = _deployment(_operator_config())
    service = SandboxInferenceService(
        SimpleNamespace(
            sandbox_config=deployment,
            databricks_store=None,
            databricks_client=None,
            provider_key_store=store,
        )
    )

    codex = await service.prepare("cloud-test", "codex-native", "owner")
    assert codex is not None
    assert codex["catalog"]["status"] == "ready"
    assert codex["catalog"]["default_model"] == CODEX_DEFAULT_MODEL
    assert codex["catalog"]["provider_label"] == "Your OpenAI key"
    # The saved profile holds the environment reference, never the key.
    assert _KEY not in json.dumps(codex)

    # The operator's own harness is untouched, and the profile still offers Codex to children.
    claude = await service.prepare("cloud-test", "claude-sdk", "owner")
    assert claude is not None and claude["catalog"]["default_model"] == _OPERATOR_MODEL
    assert "codex-native" in claude["runtime_config"]["inference"]["harnesses"]

    # An owner without a key, and a deployment without the feature, see no Codex binding.
    keyless = await service.prepare("cloud-test", "claude-sdk", "other")
    assert keyless is not None
    assert "codex-native" not in keyless["runtime_config"]["inference"]["harnesses"]
    assert keyless["configuration_revision"] != claude["configuration_revision"]
    assert deployment.default.host_config == _operator_config()


async def test_launch_environment_carries_the_key_only_where_the_profile_uses_it(
    store: OpenAIKeyStore,
) -> None:
    store.upsert("owner", api_key=_KEY)
    bound = _operator_config()
    bind_user_openai_key(bound, UserProviderKey("owner", None, "", (), 0))
    bound[HOST_LAUNCH_ENV_KEY] = {"OPERATOR_TOKEN": "trial-token"}
    deployment = _deployment(bound)

    launched = with_user_provider_keys(deployment, owner="owner", labels={}, store=store)
    on_disk, launch_env = split_launch_env(launched.default.host_config)
    # The key joins the launch-only environment and stays out of the config file.
    assert launch_env == {"OPERATOR_TOKEN": "trial-token", USER_OPENAI_KEY_ENV: _KEY}
    assert _KEY not in json.dumps(on_disk)
    assert deployment.default.host_config[HOST_LAUNCH_ENV_KEY] == {"OPERATOR_TOKEN": "trial-token"}

    unchanged = [
        # A design review sandbox never holds the owner's credentials.
        with_user_provider_keys(
            deployment, owner="owner", labels={"omnigent.egm.review": "1"}, store=store
        ),
        # An owner with no key, and a deployment without the feature.
        with_user_provider_keys(deployment, owner="other", labels={}, store=store),
        with_user_provider_keys(deployment, owner="owner", labels={}, store=None),
    ]
    assert all(result is deployment for result in unchanged)

    # A session created before the key was saved has no such provider in its profile.
    older = _deployment(_operator_config())
    assert with_user_provider_keys(older, owner="owner", labels={}, store=store) is older
