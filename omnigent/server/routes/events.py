"""Event ingest routes (``/v1/events``) feeding the server event bus.

Two entry points produce :class:`CanonicalEvent` values and publish them:

* ``POST /v1/events/github`` — a GitHub webhook. Authenticated by the
  ``X-Hub-Signature-256`` HMAC-SHA256 of the raw body under
  ``OMNIGENT_GITHUB_WEBHOOK_SECRET`` (constant-time compare). The
  ``X-GitHub-Event`` header plus the payload's ``action`` become the ``kind``
  (``check_run.completed``, ``pull_request.opened``, ``push`` …) and a small
  flat ``fields`` dict is lifted from the payload. Unknown events are
  acknowledged with 202 and ``matched: 0`` without being published.
* ``POST /v1/events/webhook`` — anything else (Slack bots, CI scripts, cron)
  under normal bearer auth: ``{"kind": str, "fields": {...}}`` with the source
  fixed to ``webhook``.

Both return ``{"matched", "delivered", "suppressed"}`` with 202.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.events import CanonicalEvent, EventBus, PublishResult
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

GITHUB_WEBHOOK_SECRET_ENV_VAR = "OMNIGENT_GITHUB_WEBHOOK_SECRET"
GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
GITHUB_EVENT_HEADER = "X-GitHub-Event"

# Largest webhook body the ingest accepts; GitHub caps payloads at 25 MB but
# the normalized fields never need more than a few KB of it.
MAX_EVENT_BODY_BYTES = 1_000_000

# GitHub event names whose ``action`` values are normalized; everything else
# is acknowledged and dropped. ``push`` has no action.
_GITHUB_ACTIONED_EVENTS: dict[str, frozenset[str]] = {
    "check_run": frozenset({"completed"}),
    "check_suite": frozenset({"completed"}),
    "pull_request": frozenset({"opened", "synchronize", "closed"}),
    "issues": frozenset({"opened", "closed"}),
    "issue_comment": frozenset({"created"}),
}
_GITHUB_ACTIONLESS_EVENTS = frozenset({"push"})
# Every GitHub event name the ingest normalizes: what a webhook should deliver.
GITHUB_INGESTED_EVENTS: tuple[str, ...] = tuple(
    sorted({*_GITHUB_ACTIONED_EVENTS, *_GITHUB_ACTIONLESS_EVENTS})
)

_KIND_PATTERN = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")
_MAX_WEBHOOK_FIELDS = 64


class WebhookEventRequest(BaseModel):
    """Body for ``POST /v1/events/webhook``."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_event(self) -> WebhookEventRequest:
        if not _KIND_PATTERN.fullmatch(self.kind):
            raise ValueError("kind must look like 'deploy' or 'deploy.finished'")
        if len(self.fields) > _MAX_WEBHOOK_FIELDS:
            raise ValueError(f"fields may have at most {_MAX_WEBHOOK_FIELDS} keys")
        return self


class PublishResponse(BaseModel):
    """Acknowledgement returned by both ingest routes."""

    matched: int
    delivered: int
    suppressed: int


def _publish_response(result: PublishResult) -> JSONResponse:
    """Render a :class:`PublishResult` as the 202 acknowledgement body."""
    body = PublishResponse(
        matched=result.matched, delivered=result.delivered, suppressed=result.suppressed
    )
    return JSONResponse(status_code=202, content=body.model_dump())


def verify_github_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """
    Check a GitHub ``X-Hub-Signature-256`` header against the raw body.

    :param body: The exact request bytes GitHub signed.
    :param signature_header: The header value, ``"sha256=<hex>"``, or ``None``.
    :param secret: The shared webhook secret.
    :returns: ``True`` only when the header carries a matching HMAC-SHA256.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=").strip()
    # Constant-time compare so a signature cannot be brute-forced byte by byte.
    return hmac.compare_digest(expected, provided)


def _repo_full_name(payload: dict[str, Any]) -> str | None:
    """``owner/name`` of the payload's repository, if present."""
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str):
            return full_name
    return None


def _sender_login(payload: dict[str, Any]) -> str | None:
    """Login of the payload's sender, if present."""
    sender = payload.get("sender")
    if isinstance(sender, dict) and isinstance(sender.get("login"), str):
        return sender["login"]
    return None


def _pull_request_numbers(payload: dict[str, Any], subject: dict[str, Any]) -> list[int]:
    """
    Pull request numbers an event concerns.

    ``pull_request`` events carry one; ``check_run`` / ``check_suite`` carry a
    list under ``pull_requests``; an ``issue_comment`` on a PR exposes it via
    ``issue.pull_request``.
    """
    numbers: list[int] = []
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        numbers.append(pull_request["number"])
    pull_requests = subject.get("pull_requests")
    if isinstance(pull_requests, list):
        numbers.extend(
            entry["number"]
            for entry in pull_requests
            if isinstance(entry, dict) and isinstance(entry.get("number"), int)
        )
    issue = payload.get("issue")
    if (
        isinstance(issue, dict)
        and isinstance(issue.get("pull_request"), dict)
        and isinstance(issue.get("number"), int)
    ):
        numbers.append(issue["number"])
    return numbers


def normalize_github_event(
    event_name: str | None, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """
    Map a GitHub webhook into ``(kind, fields)``, or ``None`` for unsupported events.

    ``kind`` is ``"<event>.<action>"`` for actioned events and the bare event
    name for ``push``. ``fields`` is a flat dict of the scalars subscriptions
    match against: ``repo`` (``owner/name``), ``action``, ``conclusion``,
    ``status``, ``name``, ``pull_request`` (number — the first when several),
    ``issue`` (number), ``merged`` (bool, ``pull_request`` events only),
    ``sender``, ``ref`` and ``url`` (the subject's ``html_url``). Absent or
    malformed values are omitted.

    :param event_name: The ``X-GitHub-Event`` header value.
    :param payload: The decoded JSON body.
    :returns: The normalized pair, or ``None`` when the event is not handled.
    """
    if not isinstance(event_name, str):
        return None
    action = payload.get("action")
    if event_name in _GITHUB_ACTIONLESS_EVENTS:
        kind = event_name
    elif event_name in _GITHUB_ACTIONED_EVENTS:
        if not isinstance(action, str) or action not in _GITHUB_ACTIONED_EVENTS[event_name]:
            return None
        kind = f"{event_name}.{action}"
    else:
        return None

    # The payload object the event is "about": check_run, pull_request, issue …
    subject_key = {"issue_comment": "comment", "issues": "issue"}.get(event_name, event_name)
    subject = payload.get(subject_key)
    if not isinstance(subject, dict):
        subject = {}

    fields: dict[str, Any] = {}
    repo = _repo_full_name(payload)
    if repo is not None:
        fields["repo"] = repo
    if isinstance(action, str):
        fields["action"] = action
    for key in ("conclusion", "status", "name"):
        value = subject.get(key)
        if isinstance(value, str):
            fields[key] = value
    pull_requests = _pull_request_numbers(payload, subject)
    if pull_requests:
        fields["pull_request"] = pull_requests[0]
    issue = payload.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("number"), int):
        fields["issue"] = issue["number"]
    if event_name == "pull_request" and isinstance(subject.get("merged"), bool):
        fields["merged"] = subject["merged"]
    sender = _sender_login(payload)
    if sender is not None:
        fields["sender"] = sender
    ref = payload.get("ref")
    if isinstance(ref, str):
        fields["ref"] = ref
    url = subject.get("html_url")
    if not isinstance(url, str) and isinstance(payload.get("repository"), dict):
        url = payload["repository"].get("html_url")
    if isinstance(url, str):
        fields["url"] = url
    return kind, fields


def create_events_router(
    bus: EventBus,
    *,
    auth_provider: AuthProvider | None = None,
    github_secret: str | None = None,
) -> APIRouter:
    """Build the event ingest router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/events/github`` and
    ``/v1/events/webhook``.

    :param bus: The event bus events are published to.
    :param auth_provider: Auth provider for the generic webhook route;
        ``None`` disables auth there.
    :param github_secret: The GitHub webhook secret; defaults to the
        ``OMNIGENT_GITHUB_WEBHOOK_SECRET`` environment variable read per
        request so a rotated secret takes effect without a restart.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _github_secret() -> str | None:
        secret = (
            github_secret
            if github_secret is not None
            else os.environ.get(GITHUB_WEBHOOK_SECRET_ENV_VAR)
        )
        return secret or None

    @router.post("/events/github", status_code=202, include_in_schema=False)
    async def ingest_github_event(request: Request) -> JSONResponse:
        """Verify, normalize and publish one GitHub webhook delivery."""
        secret = _github_secret()
        if secret is None:
            # Refuse rather than accept unsigned events when no secret is set.
            return JSONResponse(
                status_code=503,
                content={"error": f"{GITHUB_WEBHOOK_SECRET_ENV_VAR} is not configured"},
            )
        body = await request.body()
        if len(body) > MAX_EVENT_BODY_BYTES:
            raise OmnigentError("event body too large", code=ErrorCode.INVALID_INPUT)
        if not verify_github_signature(body, request.headers.get(GITHUB_SIGNATURE_HEADER), secret):
            raise OmnigentError("invalid webhook signature", code=ErrorCode.UNAUTHORIZED)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise OmnigentError("event body is not JSON", code=ErrorCode.INVALID_INPUT) from exc
        if not isinstance(payload, dict):
            raise OmnigentError("event body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        normalized = normalize_github_event(request.headers.get(GITHUB_EVENT_HEADER), payload)
        if normalized is None:
            return _publish_response(PublishResult())
        kind, fields = normalized
        result = await bus.publish(
            CanonicalEvent(source="github", kind=kind, fields=fields, received_at=int(time.time()))
        )
        return _publish_response(result)

    @router.post("/events/webhook", status_code=202)
    async def ingest_webhook_event(request: Request, body: WebhookEventRequest) -> JSONResponse:
        """Publish a caller-normalized event under bearer auth."""
        require_user(request, auth_provider)
        result = await bus.publish(
            CanonicalEvent(
                source="webhook",
                kind=body.kind,
                fields=body.fields,
                received_at=int(time.time()),
            )
        )
        return _publish_response(result)

    return router
