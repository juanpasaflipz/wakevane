"""Route tests for ``POST /v1/templates/factory``.

Real SQLite-backed agent, artifact and subscription stores, header auth, and
the in-memory GitHub webhooks API from :mod:`tests.server.github_webhook_fixtures`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.utils import installed_agent_id
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.factory_template import factory_agent_name
from omnigent.server.routes.templates import create_templates_router
from omnigent.spec import AgentSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.subscription_store.sqlalchemy_store import SqlAlchemySubscriptionStore
from tests.server.github_webhook_fixtures import (
    FakeGitHubHooks,
    connect_github,
    github_connection_store,
)

ALICE = "alice@example.com"
HEADERS = {"X-Forwarded-Email": ALICE}


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "https://omni.example.com")
    monkeypatch.setenv("OMNIGENT_GITHUB_WEBHOOK_SECRET", "test-webhook-value")


@pytest.fixture()
def env(db_uri: str, tmp_path: Path) -> dict[str, Any]:
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(ALICE, is_admin=False)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    stores: dict[str, Any] = {
        "agent": SqlAlchemyAgentStore(db_uri),
        "artifact": artifact_store,
        "subscription": SqlAlchemySubscriptionStore(db_uri),
        "github": github_connection_store(db_uri),
        "fake_github": FakeGitHubHooks(),
    }
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_templates_router(
            subscription_store=stores["subscription"],
            agent_store=stores["agent"],
            agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
            artifact_store=artifact_store,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            github_store=stores["github"],
            github_client=stores["fake_github"].client(),
        ),
        prefix="/v1",
    )
    stores["app"] = app
    stores["client"] = TestClient(app)
    return stores


def _start(env: dict[str, Any], **body: Any) -> Any:
    return env["client"].post(
        "/v1/templates/factory", json={"owner": "acme", "repo": "api", **body}, headers=HEADERS
    )


def _installed_spec(env: dict[str, Any], agent_id: str) -> AgentSpec:
    agent = env["agent"].get(agent_id)
    return validate_agent_bundle(env["artifact"].get(agent.bundle_location))


def _policy_arguments(spec: AgentSpec, path_suffix: str) -> dict[str, Any]:
    assert spec.guardrails is not None
    policy = next(p for p in spec.guardrails.policies if p.function.path.endswith(path_suffix))
    return dict(policy.function.arguments or {})


def test_factory_installs_foreman_with_default_policies(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)

    response = _start(env)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == "acme/api"
    assert body["agent_name"] == "factory-acme-api"
    assert body["agent_created"] is True
    agent = env["agent"].get(body["agent_id"])
    assert agent.kind == "user"
    assert agent.created_by == ALICE
    spec = _installed_spec(env, body["agent_id"])
    assert spec.name == "factory-acme-api"
    assert sorted(sub.name for sub in spec.sub_agents) == ["fixer", "triager", "watcher"]
    assert _policy_arguments(spec, "cost.cost_budget") == {
        "max_cost_usd": 25.0,
        "ask_thresholds_usd": [10.0],
    }
    assert _policy_arguments(spec, "orchestration.blast_radius") == {
        "gate_pushes": True,
        "ungated_sub_agents": ["fixer"],
    }
    assert _policy_arguments(spec, "orchestration.read_only_os") == {"allow_sub_agents": ["fixer"]}
    fixer = next(sub for sub in spec.sub_agents if sub.name == "fixer")
    assert fixer.os_env is not None and fixer.os_env.sandbox is not None
    assert fixer.os_env.sandbox.credential_proxy is None, "no proxy off managed sandboxes"
    assert body["hook"]["status"] == "created"
    assert len(env["fake_github"].hooks["acme/api"]) == 1


def test_factory_applies_budget_and_merge_gate(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)

    response = _start(env, daily_budget_usd=5, ask_before_merge=False)

    assert response.status_code == 200, response.text
    spec = _installed_spec(env, response.json()["agent_id"])
    assert _policy_arguments(spec, "cost.cost_budget") == {
        "max_cost_usd": 5.0,
        "ask_thresholds_usd": [2.0],
    }
    assert _policy_arguments(spec, "orchestration.blast_radius") == {
        "gate_pushes": False,
        "ungated_sub_agents": ["fixer"],
    }


def test_factory_on_managed_sandboxes_gives_the_fixer_a_github_proxy(
    env: dict[str, Any],
) -> None:
    """Managed launches run the fixer under bwrap with a writable workspace.

    Its GitHub access is the owner's token through the secretless proxy, so
    the fixer can push a branch and open the PR without the token ever
    entering the sandbox. Fails if the proxy or the writable workspace is
    dropped, which is what left the first production run unable to clone.
    """
    env["app"].state.sandbox_config = SimpleNamespace(managed_launch_supported=True)
    connect_github(env["github"], ALICE)

    response = _start(env)

    assert response.status_code == 200, response.text
    spec = _installed_spec(env, response.json()["agent_id"])
    fixer = next(sub for sub in spec.sub_agents if sub.name == "fixer")
    assert fixer.os_env is not None
    sandbox = fixer.os_env.sandbox
    assert sandbox is not None
    assert sandbox.type == "linux_bwrap"
    assert sandbox.write_paths == ["."]
    assert ".git" in (sandbox.cwd_allow_hidden or [])
    assert "* api.github.com/**" in (sandbox.egress_rules or [])
    assert sandbox.credential_proxy is not None
    hosts = {entry.host for entry in sandbox.credential_proxy.entries}
    assert hosts == {"github.com", "api.github.com"}
    assert all(
        entry.source.command == "gh auth token" for entry in sandbox.credential_proxy.entries
    )
    assert all(
        sub["execution_target"] == "managed_sandbox" for sub in response.json()["subscriptions"]
    )


def test_factory_creates_launch_subscriptions_for_the_repository(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)

    body = _start(env).json()

    subscriptions = {sub["kind"]: sub for sub in body["subscriptions"]}
    assert set(subscriptions) == {"issues.opened", "check_run.completed"}
    assert body["subscription_ids"] == [sub["id"] for sub in body["subscriptions"]]
    issues = subscriptions["issues.opened"]
    assert issues["match"] == {"repo": "acme/api"}
    assert issues["agent_id"] == body["agent_id"]
    assert issues["conversation_id"] is None
    assert issues["source"] == "github"
    assert issues["execution_target"] == "connected_host"
    assert issues["owner_user_id"] == ALICE
    assert issues["created"] is True
    assert "acme/api" in issues["prompt_template"]
    assert "{fields_json}" in issues["prompt_template"]
    checks = subscriptions["check_run.completed"]
    assert checks["match"] == {"repo": "acme/api", "conclusion": "failure"}
    assert "{fields_json}" in checks["prompt_template"]
    stored = env["subscription"].list_for_user(ALICE)
    assert sorted(sub.id for sub in stored) == sorted(body["subscription_ids"])


def test_factory_is_idempotent_per_user_and_repository(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)
    first = _start(env).json()

    second = _start(env)

    assert second.status_code == 200, second.text
    again = second.json()
    assert again["agent_id"] == first["agent_id"]
    assert again["agent_created"] is False
    assert again["subscription_ids"] == first["subscription_ids"]
    assert all(sub["created"] is False for sub in again["subscriptions"])
    assert again["hook"] == {**first["hook"], "status": "existing"}
    assert len(env["subscription"].list_for_user(ALICE)) == 2
    assert len(env["fake_github"].hooks["acme/api"]) == 1


def test_factory_repeat_with_new_budget_updates_the_same_agent(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)
    first = _start(env).json()

    again = _start(env, daily_budget_usd=50).json()

    assert again["agent_id"] == first["agent_id"]
    assert again["subscription_ids"] == first["subscription_ids"]
    spec = _installed_spec(env, again["agent_id"])
    assert _policy_arguments(spec, "cost.cost_budget")["max_cost_usd"] == 50.0


def test_factory_recreates_a_deleted_subscription(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)
    first = _start(env).json()
    env["subscription"].delete(first["subscription_ids"][0])

    again = _start(env).json()

    assert again["subscription_ids"][0] != first["subscription_ids"][0]
    assert again["subscription_ids"][1] == first["subscription_ids"][1]
    assert [sub["created"] for sub in again["subscriptions"]] == [True, False]


def test_factory_without_github_connection_creates_nothing(env: dict[str, Any]) -> None:
    response = _start(env)

    assert response.status_code == 409
    assert "Connect GitHub" in response.json()["error"]["message"]
    assert env["agent"].get(installed_agent_id(ALICE, factory_agent_name("acme", "api"))) is None
    assert env["subscription"].list_for_user(ALICE) == []


@pytest.mark.parametrize("budget", [0, -1, 5000])
def test_factory_rejects_out_of_range_budgets(env: dict[str, Any], budget: float) -> None:
    connect_github(env["github"], ALICE)

    response = _start(env, daily_budget_usd=budget)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("owner", "repo", "expected"),
    [
        ("acme", "api", "factory-acme-api"),
        ("Acme-Corp", "web.app", "factory-acme-corp-web-app"),
        ("a_b", "c--d", "factory-a_b-c--d"),
    ],
)
def test_factory_agent_name_is_a_valid_agent_name(owner: str, repo: str, expected: str) -> None:
    assert factory_agent_name(owner, repo) == expected
