"""Route tests for the one-call ladder template (``POST /v1/templates/ladder``).

Stores, auth and the in-memory GitHub webhooks API are set up as in
``tests/server/test_factory_template_routes.py``; the installed fixer is read
back through the same bundle validation the server applies.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.ladders.store import LadderStore
from omnigent.server.routes.ladder_template import create_ladder_template_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
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
        "ladder": LadderStore(db_uri),
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
        create_ladder_template_router(
            stores["ladder"],
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=stores["agent"],
            agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
            permission_store=permission_store,
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
    payload = {
        "owner": "acme",
        "repo": "api",
        "rungs": [{"model": "small-model", "max_cost_usd": 1}, {"model": "big-model"}],
        **body,
    }
    return env["client"].post("/v1/templates/ladder", json=payload, headers=HEADERS)


def _fixer_spec(env: dict[str, Any], agent_id: str) -> Any:
    agent = env["agent"].get(agent_id)
    return validate_agent_bundle(env["artifact"].get(agent.bundle_location))


def test_template_connects_the_repo_installs_a_fixer_and_creates_the_ladder(
    env: dict[str, Any],
) -> None:
    connect_github(env["github"], ALICE)

    response = _start(env, check_name="unit")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["repo"], body["agent_name"]) == ("acme/api", "ladder-fixer-acme-api")
    assert body["agent_created"] is True and body["ladder_created"] is True
    assert body["hook"]["hook_id"] in {hook["id"] for hook in env["fake_github"].hooks["acme/api"]}

    ladder = body["ladder"]
    assert (ladder["name"], ladder["repo"]) == ("acme/api: issues.opened", "acme/api")
    assert (ladder["verify_kind"], ladder["verify_name"]) == ("check_run.completed", "unit")
    assert ladder["execution_target"] == "connected_host"
    assert [
        (rung["agent_id"], rung["model_override"], rung["label"], rung["max_cost_usd"])
        for rung in ladder["rungs"]
    ] == [
        (body["agent_id"], "small-model", "small-model", 1.0),
        (body["agent_id"], "big-model", "big-model", 5.0),
    ]
    assert env["ladder"].get_ladder(ladder["id"]).user_id == ALICE

    spec = _fixer_spec(env, body["agent_id"])
    assert spec.name == "ladder-fixer-acme-api"
    assert "never ask a question" in spec.instructions
    assert spec.os_env.sandbox.credential_proxy is None, "no proxy off managed sandboxes"
    budget = next(
        policy
        for policy in spec.guardrails.policies
        if policy.function.path.endswith("cost_budget")
    )
    # One session can spend up to the highest rung cap.
    assert dict(budget.function.arguments) == {"max_cost_usd": 5.0}
    pushes = next(
        policy
        for policy in spec.guardrails.policies
        if policy.function.path.endswith("blast_radius")
    )
    assert pushes.function.arguments["gate_pushes"] is False


def test_repeated_call_returns_the_same_ladder_and_keeps_its_rungs(env: dict[str, Any]) -> None:
    connect_github(env["github"], ALICE)
    first = _start(env).json()

    again = _start(env, rungs=[{"model": "other-model"}]).json()

    assert again["agent_id"] == first["agent_id"]
    assert (again["agent_created"], again["ladder_created"]) == (False, False)
    assert again["ladder"]["id"] == first["ladder"]["id"]
    assert again["ladder"]["rungs"] == first["ladder"]["rungs"]
    assert len(env["ladder"].list_ladders_for_user(ALICE)) == 1
    assert len(env["fake_github"].hooks["acme/api"]) == 1

    other_trigger = _start(env, trigger_kind="check_run.completed").json()
    assert other_trigger["ladder_created"] is True
    assert other_trigger["ladder"]["verify_kind"] == "check_suite.completed"


def test_managed_sandbox_server_gives_the_fixer_the_github_credential_proxy(
    env: dict[str, Any],
) -> None:
    connect_github(env["github"], ALICE)
    env["app"].state.sandbox_config = SimpleNamespace(managed_launch_supported=True)

    body = _start(env).json()

    assert body["ladder"]["execution_target"] == "managed_sandbox"
    sandbox = _fixer_spec(env, body["agent_id"]).os_env.sandbox
    assert sandbox.type == "linux_bwrap"
    assert any("github.com" in rule for rule in sandbox.egress_rules)
    assert {entry.host for entry in sandbox.credential_proxy.entries} == {
        "github.com",
        "api.github.com",
    }


def test_nothing_is_created_when_github_is_not_connected(env: dict[str, Any]) -> None:
    response = _start(env)

    assert response.status_code >= 400, response.text
    assert env["ladder"].list_ladders_for_user(ALICE) == []
    assert env["fake_github"].hooks == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"rungs": []},
        {"trigger_kind": "Issues Opened"},
        {"rungs": [{"model": "m", "max_cost_usd": 0}]},
    ],
    ids=["no-rungs", "bad-trigger", "zero-cap"],
)
def test_invalid_template_requests_are_rejected(
    env: dict[str, Any], overrides: dict[str, Any]
) -> None:
    connect_github(env["github"], ALICE)
    assert _start(env, **overrides).status_code == 422
    assert env["ladder"].list_ladders_for_user(ALICE) == []
