"""Ladders end to end inside the server process.

Signed GitHub webhooks go through the real ingest route, event bus, ladder
engine, store (SQLite) and REST routes. Only the session launch is replaced:
it records the request and answers as if a session started.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.events import EventBus
from omnigent.server.ladders.engine import LadderEngine, SessionSpend
from omnigent.server.ladders.model import LadderAttempt
from omnigent.server.ladders.store import LadderStore
from omnigent.server.routes.events import create_events_router
from omnigent.server.routes.ladders import create_ladders_router
from omnigent.server.scheduled.fire import LaunchOutcome, LaunchRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.subscription_store.sqlalchemy_store import SqlAlchemySubscriptionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
_SECRET = "webhook-secret"
_REPO = "acme/api"


class _Clock:
    def __init__(self) -> None:
        # Starts at the real time so attempts fall inside the scoreboard window.
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class _Launcher:
    """Stands in for the session launch; each call gets a fresh session id."""

    def __init__(self) -> None:
        self.requests: list[LaunchRequest] = []
        self.fail_next: str | None = None

    async def __call__(self, request: LaunchRequest) -> LaunchOutcome:
        self.requests.append(request)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            return LaunchOutcome(status="failed", error=error)
        return LaunchOutcome(status="running", conversation_id=f"{len(self.requests):032x}")


class _Harness:
    def __init__(self, db_uri: str) -> None:
        self.clock = _Clock()
        self.launcher = _Launcher()
        self.spend: dict[str, SessionSpend] = {}
        self.store = LadderStore(db_uri)
        self.agents = SqlAlchemyAgentStore(db_uri)
        permissions = SqlAlchemyPermissionStore(db_uri)
        self.engine = LadderEngine(
            self.store, self.launcher, read_spend=self._read_spend, clock=self.clock
        )
        bus = EventBus(SqlAlchemySubscriptionStore(db_uri), _no_wake)
        bus.add_listener(self.engine.handle_event)
        self.app = FastAPI()

        @self.app.exception_handler(OmnigentError)
        async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
            return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

        self.app.include_router(create_events_router(bus, github_secret=_SECRET), prefix="/v1")
        self.app.include_router(
            create_ladders_router(
                self.store,
                self.engine,
                conversation_store=SqlAlchemyConversationStore(db_uri),
                agent_store=self.agents,
                permission_store=permissions,
                auth_provider=UnifiedAuthProvider(source="header"),
                read_spend=self._read_spend,
            ),
            prefix="/v1",
        )
        for user in (ALICE, BOB):
            permissions.ensure_user(user, is_admin=False)

    def _read_spend(self, attempt: LadderAttempt) -> SessionSpend | None:
        return self.spend.get(attempt.conversation_id or "")

    def agent(self, name: str) -> str:
        agent_id = generate_agent_id()
        self.agents.create(agent_id, name, f"{agent_id}/bundle")
        return agent_id


async def _no_wake(subscription: Any, text: str) -> None:
    raise AssertionError("no subscription should be woken in these tests")


@pytest.fixture()
def harness(db_uri: str) -> _Harness:
    return _Harness(db_uri)


@pytest_asyncio.fixture()
async def client(harness: _Harness) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _headers(email: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": email}


async def _github(
    client: httpx.AsyncClient, harness: _Harness, event: str, payload: dict[str, Any]
) -> None:
    body = json.dumps({"repository": {"full_name": _REPO}, **payload}).encode()
    signature = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/v1/events/github",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature},
    )
    assert response.status_code == 202, response.text
    await harness.engine.drain()


async def _issue_opened(client: httpx.AsyncClient, harness: _Harness, number: int) -> None:
    await _github(client, harness, "issues", {"action": "opened", "issue": {"number": number}})


async def _check_suite(
    client: httpx.AsyncClient, harness: _Harness, branch: str, conclusion: str
) -> None:
    await _github(
        client,
        harness,
        "check_suite",
        {
            "action": "completed",
            "check_suite": {
                "head_branch": branch,
                "conclusion": conclusion,
                "html_url": "https://github.example/checks/1",
            },
        },
    )


async def _create_ladder(
    client: httpx.AsyncClient, harness: _Harness, **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "issue fixer",
        "repo": _REPO,
        "trigger_kind": "issues.opened",
        "prompt_template": "Fix {task} in {repo}.",
        "execution_target": "connected_host",
        "rungs": [
            {"agent_id": harness.agent("small"), "label": "cheap", "max_cost_usd": 0.5},
            {"agent_id": harness.agent("big"), "label": "strong", "model_override": "big-model"},
        ],
    }
    body.update(overrides)
    response = await client.post("/v1/ladders", json=body, headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()


async def _attempts(client: httpx.AsyncClient, ladder_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/ladders/{ladder_id}/attempts", headers=_headers())
    assert response.status_code == 200, response.text
    return list(reversed(response.json()["data"]))


async def test_failed_check_escalates_and_passing_check_ends_the_task(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness)
    cheap_agent, strong_agent = (rung["agent_id"] for rung in ladder["rungs"])

    await _issue_opened(client, harness, 42)

    (first,) = await _attempts(client, ladder["id"])
    assert (first["status"], first["rung_index"], first["task_key"]) == ("running", 0, "issue:42")
    assert first["branch"] == f"wakevane/att-{first['id']}"
    launch = harness.launcher.requests[0]
    assert (launch.agent_id, launch.owner_user_id, launch.max_cost_usd) == (
        cheap_agent,
        ALICE,
        0.5,
    )
    assert launch.prompt.startswith("Fix issue:42 in acme/api.")
    assert f"`{first['branch']}`" in launch.prompt and "rung 1 of 2" in launch.prompt
    assert launch.labels["omnigent.ladder_attempt"] == first["id"]

    # The attempt opens a pull request, then its checks fail.
    await _github(
        client,
        harness,
        "pull_request",
        {"action": "opened", "pull_request": {"number": 77, "head": {"ref": first["branch"]}}},
    )
    harness.spend[first["conversation_id"]] = SessionSpend(cost_usd=0.25, model="small-model")
    await _check_suite(client, harness, first["branch"], "failure")

    first, second = await _attempts(client, ladder["id"])
    assert (first["status"], first["pull_request"], first["cost_usd"]) == ("failed", 77, 0.25)
    assert first["model"] == "small-model"
    assert "concluded failure" in first["detail"]
    assert (second["status"], second["rung_index"]) == ("running", 1)
    assert second["task_id"] == first["task_id"]
    escalation = harness.launcher.requests[1]
    assert (escalation.agent_id, escalation.model_override) == (strong_agent, "big-model")
    assert f"`{first['branch']}` (pull request #77)" in escalation.prompt
    assert "concluded failure" in escalation.prompt

    harness.spend[second["conversation_id"]] = SessionSpend(cost_usd=1.5, model="big-model")
    await _check_suite(client, harness, second["branch"], "success")
    # A redelivered verdict changes nothing and starts nothing.
    await _check_suite(client, harness, second["branch"], "failure")

    _, second = await _attempts(client, ladder["id"])
    assert second["status"] == "passed"
    assert len(harness.launcher.requests) == 2

    board = (await client.get("/v1/ladders/scoreboard", headers=_headers())).json()
    assert board["tasks"]["solved"] == 1 and board["tasks"]["solved_by_rung"] == [0, 1]
    assert board["tasks"]["cost_per_solved_task"] == pytest.approx(1.75)
    assert [row["pass_rate"] for row in board["rungs"]] == [0.0, 1.0]
    assert [row["label"] for row in board["rungs"]] == ["cheap", "strong"]


async def test_last_rung_failing_leaves_the_task_unsolved(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness)
    await _issue_opened(client, harness, 1)
    for _ in range(2):
        running = (await _attempts(client, ladder["id"]))[-1]
        await _check_suite(client, harness, running["branch"], "failure")

    attempts = await _attempts(client, ladder["id"])
    assert [attempt["status"] for attempt in attempts] == ["failed", "failed"]
    assert len(harness.launcher.requests) == 2
    board = (await client.get("/v1/ladders/scoreboard", headers=_headers())).json()
    assert (board["tasks"]["solved"], board["tasks"]["unsolved"]) == (0, 1)


async def test_silent_attempt_times_out_and_escalates(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness, attempt_timeout_minutes=30)
    await _issue_opened(client, harness, 5)

    harness.clock.now += 29 * 60
    assert [a["status"] for a in await _attempts(client, ladder["id"])] == ["running"]

    harness.clock.now += 2 * 60
    attempts = await _attempts(client, ladder["id"])
    await harness.engine.drain()
    assert attempts[0]["status"] == "timed_out"
    assert "30 minutes" in attempts[0]["detail"]
    assert [a["rung_index"] for a in await _attempts(client, ladder["id"])] == [0, 1]


async def test_events_that_must_not_start_or_judge_work(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness, trigger_match={"sender": "octocat"})

    # Wrong sender for the ladder's filter.
    await _issue_opened(client, harness, 9)
    assert harness.launcher.requests == []

    await _github(
        client,
        harness,
        "issues",
        {"action": "opened", "issue": {"number": 9}, "sender": {"login": "octocat"}},
    )
    (attempt,) = await _attempts(client, ladder["id"])

    # A second event for the same issue while the first attempt runs.
    await _github(
        client,
        harness,
        "issues",
        {"action": "opened", "issue": {"number": 9}, "sender": {"login": "octocat"}},
    )
    # Checks on someone else's branch, a neutral conclusion, and the right
    # branch in another repository.
    await _check_suite(client, harness, "main", "failure")
    await _check_suite(client, harness, attempt["branch"], "neutral")
    await _github(
        client,
        harness,
        "check_suite",
        {
            "action": "completed",
            "repository": {"full_name": "evil/fork"},
            "check_suite": {"head_branch": attempt["branch"], "conclusion": "success"},
        },
    )

    assert [a["status"] for a in await _attempts(client, ladder["id"])] == ["running"]
    assert len(harness.launcher.requests) == 1


async def test_named_check_run_is_the_only_verdict_when_configured(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(
        client, harness, verify_kind="check_run.completed", verify_name="unit"
    )
    await _issue_opened(client, harness, 3)
    (attempt,) = await _attempts(client, ladder["id"])

    def check_run(name: str, conclusion: str) -> dict[str, Any]:
        return {
            "action": "completed",
            "check_run": {
                "name": name,
                "conclusion": conclusion,
                "check_suite": {"head_branch": attempt["branch"]},
            },
        }

    await _github(client, harness, "check_run", check_run("lint", "failure"))
    await _check_suite(client, harness, attempt["branch"], "failure")
    assert (await _attempts(client, ladder["id"]))[0]["status"] == "running"

    await _github(client, harness, "check_run", check_run("unit", "success"))
    (attempt,) = await _attempts(client, ladder["id"])
    assert (attempt["status"], attempt["detail"]) == ("passed", "unit passed")


async def test_launch_failure_is_recorded_and_does_not_escalate(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness)
    harness.launcher.fail_next = "no online host"
    await _issue_opened(client, harness, 8)

    (attempt,) = await _attempts(client, ladder["id"])
    assert (attempt["status"], attempt["detail"]) == ("launch_failed", "no online host")
    assert len(harness.launcher.requests) == 1


async def test_paused_ladder_starts_nothing_and_stops_escalating(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness)
    await _issue_opened(client, harness, 1)
    (attempt,) = await _attempts(client, ladder["id"])

    paused = await client.patch(
        f"/v1/ladders/{ladder['id']}", json={"state": "paused"}, headers=_headers()
    )
    assert paused.json()["state"] == "paused"
    await _issue_opened(client, harness, 2)
    await _check_suite(client, harness, attempt["branch"], "failure")

    assert [a["status"] for a in await _attempts(client, ladder["id"])] == ["failed"]
    assert len(harness.launcher.requests) == 1


async def test_ladders_are_private_to_their_owner(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    ladder = await _create_ladder(client, harness)
    await _issue_opened(client, harness, 1)

    for path in (f"/v1/ladders/{ladder['id']}", f"/v1/ladders/{ladder['id']}/attempts"):
        assert (await client.get(path, headers=_headers(BOB))).status_code == 404
    assert (await client.get("/v1/ladders", headers=_headers(BOB))).json() == {"data": []}
    board = (await client.get("/v1/ladders/scoreboard", headers=_headers(BOB))).json()
    assert board["tasks"]["total"] == 0
    refused = await client.delete(f"/v1/ladders/{ladder['id']}", headers=_headers(BOB))
    assert refused.status_code == 404

    deleted = await client.delete(f"/v1/ladders/{ladder['id']}", headers=_headers())
    assert deleted.json() == {"id": ladder["id"], "deleted": True}
    assert (await client.get(f"/v1/ladders/{ladder['id']}", headers=_headers())).status_code == 404


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repo": "not-a-repo"}, "owner/name"),
        ({"verify_kind": "push"}, "verify_kind"),
        ({"verify_name": "unit"}, "verify_name only applies"),
        ({"rungs": []}, "rungs"),
        ({"execution_target": "the_cloud"}, "execution_target"),
        ({"execution_target": "managed_sandbox"}, "managed sandboxes are not configured"),
    ],
)
async def test_invalid_ladders_are_rejected(
    client: httpx.AsyncClient, harness: _Harness, overrides: dict[str, Any], message: str
) -> None:
    body = {
        "name": "x",
        "repo": _REPO,
        "trigger_kind": "issues.opened",
        "prompt_template": "Fix it",
        "execution_target": "connected_host",
        "rungs": [{"agent_id": harness.agent("small")}],
        **overrides,
    }
    response = await client.post("/v1/ladders", json=body, headers=_headers())
    assert response.status_code in (400, 422), response.text
    assert message in response.text


async def test_unknown_agent_on_a_rung_is_rejected(
    client: httpx.AsyncClient, harness: _Harness
) -> None:
    body = {
        "name": "x",
        "repo": _REPO,
        "trigger_kind": "issues.opened",
        "prompt_template": "Fix it",
        "execution_target": "connected_host",
        "rungs": [{"agent_id": generate_agent_id()}],
    }
    response = await client.post("/v1/ladders", json=body, headers=_headers())
    assert response.status_code in (403, 404), response.text
    assert (await client.get("/v1/ladders", headers=_headers())).json() == {"data": []}
