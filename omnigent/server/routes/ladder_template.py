"""``POST /v1/templates/ladder`` — start a ladder on a repository in one call.

Connects the repository's webhook, installs a fixer agent for it as the
caller's own agent and creates a ladder that runs that fixer once per model,
cheapest first. See :mod:`omnigent.server.ladders.template`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.db.utils import installed_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.github_webhooks import connect_repository
from omnigent.server.ladders.model import (
    DEFAULT_ATTEMPT_TIMEOUT_MINUTES,
    MAX_RUNGS,
    LadderState,
    Rung,
)
from omnigent.server.ladders.store import LadderStore
from omnigent.server.ladders.template import (
    DEFAULT_RUNG_MAX_COST_USD,
    DEFAULT_TRIGGER_KIND,
    LADDER_PROMPT_TEMPLATE,
    MAX_RUNG_MAX_COST_USD,
    build_ladder_fixer_bundle,
    ladder_fixer_agent_name,
    ladder_name,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._launch_target import validate_launch_target
from omnigent.server.routes.builtin_agents import install_user_agent
from omnigent.server.routes.ladders import ladder_response
from omnigent.server.routes.subscriptions import KIND_PATTERN
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore

_MAX_GITHUB_NAME_CHARS = 100


class LadderTemplateRungRequest(BaseModel):
    """One step of ``POST /v1/templates/ladder``."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    max_cost_usd: float = Field(default=DEFAULT_RUNG_MAX_COST_USD, gt=0, le=MAX_RUNG_MAX_COST_USD)


class LadderTemplateRequest(BaseModel):
    """Body of ``POST /v1/templates/ladder``."""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)
    repo: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)
    rungs: list[LadderTemplateRungRequest] = Field(min_length=1, max_length=MAX_RUNGS)
    trigger_kind: str = DEFAULT_TRIGGER_KIND
    # Name of the one check that judges an attempt; omitted, any check suite does.
    check_name: str | None = Field(default=None, min_length=1, max_length=256)
    attempt_timeout_minutes: int = Field(default=DEFAULT_ATTEMPT_TIMEOUT_MINUTES, ge=5, le=1440)

    @model_validator(mode="after")
    def _validate(self) -> LadderTemplateRequest:
        if not KIND_PATTERN.fullmatch(self.trigger_kind):
            raise ValueError("trigger_kind must look like 'issues.opened'")
        return self


def create_ladder_template_router(
    store: LadderStore,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    agent_cache: Any,
    permission_store: PermissionStore | None = None,
    artifact_store: ArtifactStore | None = None,
    auth_provider: AuthProvider | None = None,
    github_store: Any | None = None,
    github_client: Any | None = None,
) -> APIRouter:
    """Build the ladder template router, mounted with ``prefix="/v1"``.

    :param store: Holds the ladder the template creates.
    :param agent_store: Stores the installed fixer (needs user-agent support).
    :param agent_cache: Evicted when a repeated call changes the fixer.
    :param artifact_store: Holds the fixer's bundle; ``None`` answers 409.
    :param github_store: Per-user GitHub connections for the webhook step.
    :param github_client: GitHub App client for the webhook step.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/templates/ladder")
    async def start_ladder(request: Request, body: LadderTemplateRequest) -> dict[str, Any]:
        """Start (or return the existing) ladder for ``owner/repo`` and its trigger.

        Idempotent per caller, repository and trigger: a repeated call
        reinstalls the fixer and returns the ladder it already created
        without changing its rungs.
        """
        user_id = require_user(request, auth_provider)
        owner_id = None if user_id in (None, RESERVED_USER_LOCAL) else user_id
        if artifact_store is None or not agent_store.supports_user_agents:
            raise OmnigentError(
                "this server cannot install custom agents, so the ladder template is unavailable",
                code=ErrorCode.CONFLICT,
            )
        # Connect first: every precondition failure surfaces before anything is created.
        hook = await connect_repository(
            request, user_id, body.owner, body.repo, store=github_store, client=github_client
        )
        sandbox_config = getattr(request.app.state, "sandbox_config", None)
        execution_target = (
            "managed_sandbox"
            if sandbox_config is not None and sandbox_config.managed_launch_supported
            else "connected_host"
        )
        agent_name = ladder_fixer_agent_name(body.owner, body.repo)
        bundle = await asyncio.to_thread(
            build_ladder_fixer_bundle,
            body.owner,
            body.repo,
            max_cost_usd=max(rung.max_cost_usd for rung in body.rungs),
            managed_sandbox=execution_target == "managed_sandbox",
        )
        await asyncio.to_thread(validate_agent_bundle, bundle, enforce_handler_allowlist=True)
        agent_existed = (
            await asyncio.to_thread(agent_store.get, installed_agent_id(user_id, agent_name))
            is not None
        )
        agent = await asyncio.to_thread(
            install_user_agent,
            agent_store,
            artifact_store,
            agent_cache,
            owner=user_id,
            name=agent_name,
            bundle_bytes=bundle,
        )
        full_name = f"{body.owner}/{body.repo}"
        name = ladder_name(body.owner, body.repo, body.trigger_kind)
        existing = next(
            (
                ladder
                for ladder in store.list_ladders_for_user(owner_id)
                if ladder.name == name and ladder.repo == full_name
            ),
            None,
        )
        if existing is None:
            rungs: list[Rung] = []
            for rung in body.rungs:
                agent_id, _workspace, model = await validate_launch_target(
                    request,
                    owner_id=owner_id,
                    agent_id=agent.id,
                    execution_target=execution_target,
                    host_id=None,
                    workspace=None,
                    model_override=rung.model,
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
                        model_override=model,
                        max_cost_usd=rung.max_cost_usd,
                        label=rung.model,
                    )
                )
            ladder = store.create_ladder(
                uuid.uuid4().hex,
                user_id=owner_id,
                name=name,
                repo=full_name,
                trigger_kind=body.trigger_kind,
                trigger_match={},
                prompt_template=LADDER_PROMPT_TEMPLATE,
                rungs=rungs,
                verify_kind=(
                    "check_run.completed" if body.check_name else "check_suite.completed"
                ),
                verify_name=body.check_name,
                execution_target=execution_target,
                host_id=None,
                workspace=None,
                attempt_timeout_minutes=body.attempt_timeout_minutes,
            )
        else:
            ladder = existing
        return {
            "repo": full_name,
            "agent_id": agent.id,
            "agent_name": agent.name,
            "agent_created": not agent_existed,
            "ladder": ladder_response(ladder),
            "ladder_created": existing is None,
            "ladder_active": ladder.state == LadderState.ACTIVE.value,
            "hook": hook.to_response(),
        }

    return router
