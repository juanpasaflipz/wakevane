"""Tests for the event ingest routes (``omnigent.server.routes.events``).

Mounts the router on a bare FastAPI app with a fake bus so the tests cover
signature verification, GitHub payload normalization and the generic webhook
without a database or the full ``create_app`` surface.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.events import CanonicalEvent, PublishResult, event_matches
from omnigent.server.routes.events import (
    GITHUB_WEBHOOK_SECRET_ENV_VAR,
    create_events_router,
    normalize_github_event,
    verify_github_signature,
)

_SECRET = "s3cr3t"


class FakeBus:
    """Records published events and returns a scripted result."""

    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    async def publish(self, event: CanonicalEvent) -> PublishResult:
        self.events.append(event)
        return PublishResult(matched=2, delivered=1, suppressed=1)


def _app(bus: FakeBus, *, secret: str | None = _SECRET) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

    app.include_router(create_events_router(bus, github_secret=secret), prefix="/v1")
    return app


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_github(
    client: TestClient,
    event: str,
    payload: dict[str, Any],
    *,
    signature: str | None = "valid",
) -> Any:
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature == "valid":
        headers["X-Hub-Signature-256"] = _sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/v1/events/github", content=body, headers=headers)


_CHECK_RUN = {
    "action": "completed",
    "check_run": {
        "name": "unit",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://github.com/acme/api/runs/1",
        "pull_requests": [{"number": 412}, {"number": 413}],
    },
    "repository": {"full_name": "acme/api", "html_url": "https://github.com/acme/api"},
    "sender": {"login": "octocat"},
}

_PULL_REQUEST = {
    "action": "opened",
    "number": 77,
    "pull_request": {
        "number": 77,
        "html_url": "https://github.com/acme/api/pull/77",
        "state": "open",
    },
    "repository": {"full_name": "acme/api"},
    "sender": {"login": "hubot"},
}


# ── signature verification ───────────────────────────────────────────────────


def test_verify_signature_accepts_matching_hmac_and_rejects_others() -> None:
    body = b'{"a": 1}'
    assert verify_github_signature(body, _sign(body), _SECRET) is True
    assert verify_github_signature(body, _sign(body, "other"), _SECRET) is False
    assert verify_github_signature(body, "sha1=abc", _SECRET) is False
    assert verify_github_signature(body, None, _SECRET) is False


def test_valid_signature_publishes_and_returns_counts() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    response = _post_github(client, "check_run", _CHECK_RUN)

    assert response.status_code == 202, response.text
    assert response.json() == {"matched": 2, "delivered": 1, "suppressed": 1}
    assert len(bus.events) == 1


def test_invalid_signature_is_401_and_not_published() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    tampered = _post_github(client, "check_run", _CHECK_RUN, signature=_sign(b"other body"))
    missing = _post_github(client, "check_run", _CHECK_RUN, signature=None)

    assert tampered.status_code == 401
    assert missing.status_code == 401
    assert bus.events == []


def test_missing_secret_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_WEBHOOK_SECRET_ENV_VAR, raising=False)
    bus = FakeBus()
    client = TestClient(_app(bus, secret=None))

    response = _post_github(client, "check_run", _CHECK_RUN)

    assert response.status_code == 503
    assert GITHUB_WEBHOOK_SECRET_ENV_VAR in response.json()["error"]
    assert bus.events == []


def test_secret_falls_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_WEBHOOK_SECRET_ENV_VAR, _SECRET)
    bus = FakeBus()
    client = TestClient(_app(bus, secret=None))

    assert _post_github(client, "push", {"ref": "refs/heads/main"}).status_code == 202
    assert len(bus.events) == 1


# ── normalization ────────────────────────────────────────────────────────────


def test_check_run_normalizes_kind_and_fields() -> None:
    normalized = normalize_github_event("check_run", _CHECK_RUN)
    assert normalized is not None
    kind, fields = normalized
    assert kind == "check_run.completed"
    assert fields == {
        "repo": "acme/api",
        "action": "completed",
        "conclusion": "failure",
        "status": "completed",
        "name": "unit",
        "pull_request": 412,
        "sender": "octocat",
        "url": "https://github.com/acme/api/runs/1",
    }


def test_pull_request_normalizes_kind_and_fields() -> None:
    normalized = normalize_github_event("pull_request", _PULL_REQUEST)
    assert normalized is not None
    kind, fields = normalized
    assert kind == "pull_request.opened"
    assert fields == {
        "repo": "acme/api",
        "action": "opened",
        "pull_request": 77,
        "sender": "hubot",
        "url": "https://github.com/acme/api/pull/77",
    }


def _closed_pull_request(**pr_overrides: Any) -> dict[str, Any]:
    return {
        "action": "closed",
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 9, **pr_overrides},
    }


@pytest.mark.parametrize("merged", [True, False])
def test_pull_request_closed_preserves_boolean_merged(merged: bool) -> None:
    normalized = normalize_github_event("pull_request", _closed_pull_request(merged=merged))
    assert normalized is not None
    kind, fields = normalized
    assert kind == "pull_request.closed"
    assert fields["merged"] is merged


@pytest.mark.parametrize("pr_overrides", [{}, {"merged": None}, {"merged": "true"}, {"merged": 1}])
def test_pull_request_omits_absent_or_malformed_merged(pr_overrides: dict[str, Any]) -> None:
    normalized = normalize_github_event("pull_request", _closed_pull_request(**pr_overrides))
    assert normalized is not None
    assert "merged" not in normalized[1]


def test_merged_only_subscription_matches_merged_and_rejects_unmerged() -> None:
    merged = normalize_github_event("pull_request", _closed_pull_request(merged=True))
    unmerged = normalize_github_event("pull_request", _closed_pull_request(merged=False))
    assert merged is not None and unmerged is not None
    assert event_matches({"merged": True}, merged[1]) is True
    assert event_matches({"merged": True}, unmerged[1]) is False


def test_issue_comment_on_pull_request_exposes_both_numbers() -> None:
    payload = {
        "action": "created",
        "issue": {"number": 9, "pull_request": {"url": "..."}},
        "comment": {"html_url": "https://github.com/acme/api/pull/9#c1"},
        "repository": {"full_name": "acme/api"},
    }
    normalized = normalize_github_event("issue_comment", payload)
    assert normalized == (
        "issue_comment.created",
        {
            "repo": "acme/api",
            "action": "created",
            "pull_request": 9,
            "issue": 9,
            "url": "https://github.com/acme/api/pull/9#c1",
        },
    )


def test_push_has_no_action_and_carries_ref() -> None:
    payload = {"ref": "refs/heads/main", "repository": {"full_name": "acme/api"}}
    assert normalize_github_event("push", payload) == (
        "push",
        {"repo": "acme/api", "ref": "refs/heads/main"},
    )


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("star", {"action": "created"}),  # unsupported event name
        ("pull_request", {"action": "labeled"}),  # unsupported action
        ("check_run", {}),  # missing action
        (None, {"action": "completed"}),  # missing header
    ],
)
def test_unsupported_events_normalize_to_none(event: str | None, payload: dict[str, Any]) -> None:
    assert normalize_github_event(event, payload) is None


def test_unknown_event_is_acknowledged_with_zero_matches_and_not_published() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    response = _post_github(client, "star", {"action": "created"})

    assert response.status_code == 202
    assert response.json() == {"matched": 0, "delivered": 0, "suppressed": 0}
    assert bus.events == []


def test_published_event_has_github_source_and_received_at() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    _post_github(client, "pull_request", _PULL_REQUEST)

    (event,) = bus.events
    assert event.source == "github"
    assert event.kind == "pull_request.opened"
    assert event.fields["pull_request"] == 77
    assert event.received_at > 0


def test_non_json_body_with_valid_signature_is_400() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))
    body = b"not json"

    response = client.post(
        "/v1/events/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 400
    assert bus.events == []


# ── generic webhook ──────────────────────────────────────────────────────────


def test_webhook_publishes_with_fixed_source() -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    response = client.post(
        "/v1/events/webhook",
        json={"kind": "deploy.finished", "fields": {"env": "prod", "ok": True}},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {"matched": 2, "delivered": 1, "suppressed": 1}
    (event,) = bus.events
    assert (event.source, event.kind, event.fields) == (
        "webhook",
        "deploy.finished",
        {"env": "prod", "ok": True},
    )


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "Deploy Finished"},  # bad kind shape
        {"kind": "deploy", "fields": {}, "extra": 1},  # unknown field
        {"fields": {}},  # missing kind
    ],
)
def test_webhook_rejects_malformed_bodies(body: dict[str, Any]) -> None:
    bus = FakeBus()
    client = TestClient(_app(bus))

    assert client.post("/v1/events/webhook", json=body).status_code == 422
    assert bus.events == []
