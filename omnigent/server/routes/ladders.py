"""REST API for ladders — ``/v1/ladders``.

A ladder works one kind of event in one repository with an ordered list of
agents, cheapest first, and lets the repository's own checks judge each
attempt. These endpoints create, list, pause and delete ladders, list their
attempts and serve the per-repository scoreboard.

Ownership mirrors subscriptions: rows are scoped to the calling user and a
ladder owned by someone else 404s. Every rung needs the same access to its
agent (and pinned host or workspace) that a launch subscription needs.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.ladders.engine import LadderEngine, SpendReader
from omnigent.server.ladders.model import (
    DEFAULT_ATTEMPT_TIMEOUT_MINUTES,
    DEFAULT_VERIFY_KIND,
    MAX_RUNGS,
    VERIFY_KINDS,
    Ladder,
    LadderAttempt,
    LadderState,
    Rung,
)
from omnigent.server.ladders.scoreboard import build_scoreboard
from omnigent.server.ladders.store import LadderStore
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._launch_target import validate_launch_target
from omnigent.server.routes.subscriptions import (
    KIND_PATTERN,
    MAX_PROMPT_TEMPLATE_CHARS,
    _validate_match,
)
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SECONDS_PER_DAY = 86_400
_MAX_ATTEMPTS_PAGE = 500


class RungRequest(BaseModel):
    """One rung of ``POST /v1/ladders``."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    model_override: str | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)
    label: str | None = Field(default=None, max_length=64)


class CreateLadderRequest(BaseModel):
    """Body of ``POST /v1/ladders``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    repo: str = Field(max_length=256)
    trigger_kind: str
    trigger_match: dict[str, Any] | None = None
    prompt_template: str = Field(min_length=1, max_length=MAX_PROMPT_TEMPLATE_CHARS)
    rungs: list[RungRequest] = Field(min_length=1, max_length=MAX_RUNGS)
    verify_kind: str = DEFAULT_VERIFY_KIND
    verify_name: str | None = Field(default=None, min_length=1, max_length=256)
    execution_target: str = "managed_sandbox"
    host_id: str | None = Field(default=None, min_length=1)
    workspace: str | None = Field(default=None, min_length=1)
    attempt_timeout_minutes: int = Field(default=DEFAULT_ATTEMPT_TIMEOUT_MINUTES, ge=5, le=1440)

    @model_validator(mode="after")
    def _validate_create(self) -> CreateLadderRequest:
        if not _REPO_PATTERN.fullmatch(self.repo):
            raise ValueError("repo must look like 'owner/name'")
        if not KIND_PATTERN.fullmatch(self.trigger_kind):
            raise ValueError("trigger_kind must look like 'issues.opened'")
        if self.verify_kind not in VERIFY_KINDS:
            raise ValueError(f"verify_kind must be one of {sorted(VERIFY_KINDS)}")
        if self.verify_name is not None and self.verify_kind != "check_run.completed":
            raise ValueError("verify_name only applies to verify_kind 'check_run.completed'")
        self.trigger_match = _validate_match(self.trigger_match)
        return self


class UpdateLadderRequest(BaseModel):
    """Body of ``PATCH /v1/ladders/{id}``."""

    model_config = ConfigDict(extra="forbid")

    state: str

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateLadderRequest:
        if self.state not in {LadderState.ACTIVE.value, LadderState.PAUSED.value}:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete")
        return self


def ladder_response(ladder: Ladder) -> dict[str, Any]:
    """Serialize a ladder for the API."""
    return {
        "id": ladder.id,
        "owner_user_id": ladder.user_id,
        "name": ladder.name,
        "repo": ladder.repo,
        "trigger_kind": ladder.trigger_kind,
        "trigger_match": ladder.trigger_match,
        "prompt_template": ladder.prompt_template,
        "rungs": [rung.to_json() for rung in ladder.rungs],
        "verify_kind": ladder.verify_kind,
        "verify_name": ladder.verify_name,
        "execution_target": ladder.execution_target,
        "host_id": ladder.host_id,
        "workspace": ladder.workspace,
        "attempt_timeout_minutes": ladder.attempt_timeout_minutes,
        "state": ladder.state,
        "created_at": ladder.created_at,
        "updated_at": ladder.updated_at,
    }


def attempt_response(attempt: LadderAttempt) -> dict[str, Any]:
    """Serialize an attempt for the API."""
    return {
        "id": attempt.id,
        "ladder_id": attempt.ladder_id,
        "task_id": attempt.task_id,
        "repo": attempt.repo,
        "task_key": attempt.task_key,
        "rung_index": attempt.rung_index,
        "agent_id": attempt.agent_id,
        "model_override": attempt.model_override,
        "model": attempt.model,
        "branch": attempt.branch,
        "conversation_id": attempt.conversation_id,
        "pull_request": attempt.pull_request,
        "status": attempt.status,
        "detail": attempt.detail,
        "cost_usd": attempt.cost_usd,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


def create_ladders_router(
    store: LadderStore,
    engine: LadderEngine,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    agent_cache: Any | None = None,
    artifact_store: ArtifactStore | None = None,
    read_spend: SpendReader | None = None,
) -> APIRouter:
    """Create the ladders router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/ladders[/…]``.

    :param store: Ladder and attempt persistence.
    :param engine: The running engine; reads sweep stale attempts through it.
    :param read_spend: Reads an attempt's current session cost for the
        scoreboard; ``None`` serves the cost recorded when the attempt ended.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner_id(request: Request) -> str | None:
        user_id = require_user(request, auth_provider)
        return None if user_id in (None, RESERVED_USER_LOCAL) else user_id

    def _require_owned(ladder_id: str, owner_id: str | None) -> Ladder:
        ladder = store.get_ladder(ladder_id)
        if (
            ladder is None
            or ladder.state == LadderState.DELETED.value
            or ladder.user_id != owner_id
        ):
            raise OmnigentError("Ladder not found", code=ErrorCode.NOT_FOUND)
        return ladder

    def _with_current_spend(attempt: LadderAttempt) -> LadderAttempt:
        """Refresh an attempt's cost from its session when it can be read."""
        if read_spend is None or attempt.conversation_id is None:
            return attempt
        spend = read_spend(attempt)
        if spend is None or spend.cost_usd is None:
            return attempt
        return replace(attempt, cost_usd=spend.cost_usd, model=spend.model or attempt.model)

    @router.post("/ladders")
    async def create_ladder(request: Request, body: CreateLadderRequest) -> dict[str, Any]:
        """Create a ladder; every rung is authorized like a launch subscription."""
        owner_id = _owner_id(request)
        rungs: list[Rung] = []
        workspace = body.workspace
        for rung in body.rungs:
            agent_id, workspace, model_override = await validate_launch_target(
                request,
                owner_id=owner_id,
                agent_id=rung.agent_id,
                execution_target=body.execution_target,
                host_id=body.host_id,
                workspace=body.workspace,
                model_override=rung.model_override,
                agent_store=agent_store,
                conversation_store=conversation_store,
                permission_store=permission_store,
                agent_cache=agent_cache,
                artifact_store=artifact_store,
                noun="ladder",
            )
            rungs.append(
                Rung(
                    agent_id=agent_id,
                    model_override=model_override,
                    max_cost_usd=rung.max_cost_usd,
                    label=rung.label,
                )
            )
        ladder = store.create_ladder(
            uuid.uuid4().hex,
            user_id=owner_id,
            name=body.name,
            repo=body.repo,
            trigger_kind=body.trigger_kind,
            trigger_match=body.trigger_match or {},
            prompt_template=body.prompt_template,
            rungs=rungs,
            verify_kind=body.verify_kind,
            verify_name=body.verify_name,
            execution_target=body.execution_target,
            host_id=body.host_id,
            workspace=workspace,
            attempt_timeout_minutes=body.attempt_timeout_minutes,
        )
        return ladder_response(ladder)

    @router.get("/ladders")
    async def list_ladders(request: Request) -> dict[str, Any]:
        """List the caller's ladders, oldest first."""
        ladders = store.list_ladders_for_user(_owner_id(request))
        return {"data": [ladder_response(ladder) for ladder in ladders]}

    # Declared before ``/ladders/{ladder_id}`` so the literal path wins.
    @router.get("/ladders/scoreboard")
    async def get_scoreboard(
        request: Request,
        days: int = Query(default=30, ge=1, le=366),
        repo: str | None = Query(default=None, max_length=256),
    ) -> dict[str, Any]:
        """
        Score the caller's ladders: tasks solved, cost per solved task, and
        pass rate and cost for each rung.

        :param days: Window in days ending now.
        :param repo: Only attempts in this repository, e.g. ``acme/api``.
        """
        owner_id = _owner_id(request)
        await engine.sweep()

        def build() -> dict[str, Any]:
            attempts = store.list_attempts_for_user(
                owner_id, started_since=int(time.time()) - days * _SECONDS_PER_DAY, repo=repo
            )
            ladders = {ladder.id: ladder for ladder in store.list_ladders_for_user(owner_id)}
            return build_scoreboard(
                (_with_current_spend(attempt) for attempt in attempts),
                ladders,
                window_days=days,
                repo=repo,
            )

        return await asyncio.to_thread(build)

    @router.get("/ladders/{ladder_id}")
    async def get_ladder(request: Request, ladder_id: str) -> dict[str, Any]:
        """Read one ladder."""
        return ladder_response(_require_owned(ladder_id, _owner_id(request)))

    @router.patch("/ladders/{ladder_id}")
    async def update_ladder(
        request: Request, ladder_id: str, body: UpdateLadderRequest
    ) -> dict[str, Any]:
        """Pause or resume a ladder. A paused ladder starts and escalates nothing."""
        _require_owned(ladder_id, _owner_id(request))
        updated = store.set_ladder_state(ladder_id, body.state)
        if updated is None:
            raise OmnigentError("Ladder not found", code=ErrorCode.NOT_FOUND)
        return ladder_response(updated)

    @router.delete("/ladders/{ladder_id}")
    async def delete_ladder(request: Request, ladder_id: str) -> dict[str, Any]:
        """Delete a ladder. Sessions it already started keep running."""
        _require_owned(ladder_id, _owner_id(request))
        store.set_ladder_state(ladder_id, LadderState.DELETED.value)
        return {"id": ladder_id, "deleted": True}

    @router.get("/ladders/{ladder_id}/attempts")
    async def list_attempts(
        request: Request,
        ladder_id: str,
        limit: int = Query(default=100, ge=1, le=_MAX_ATTEMPTS_PAGE),
    ) -> dict[str, Any]:
        """List a ladder's attempts, newest first."""
        _require_owned(ladder_id, _owner_id(request))
        await engine.sweep()
        attempts = store.list_attempts_for_ladder(ladder_id, limit=limit)
        return {"data": [attempt_response(attempt) for attempt in attempts]}

    return router
