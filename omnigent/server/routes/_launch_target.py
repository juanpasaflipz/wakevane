"""Authorize where an event-triggered session may run.

Launch subscriptions and ladder rungs both start a fresh session for their
owner on every matching event, so they share one check of the agent, host,
workspace and sandbox the session would use.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request

from omnigent.entities.subscription import SUBSCRIPTION_EXECUTION_TARGETS
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.bundles import agent_for_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore


async def validate_launch_target(
    request: Request,
    *,
    owner_id: str | None,
    agent_id: str,
    execution_target: str,
    host_id: str | None,
    workspace: str | None,
    model_override: str | None,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    agent_cache: Any | None,
    artifact_store: ArtifactStore | None,
    noun: str = "subscription",
) -> tuple[str, str | None, str | None]:
    """Authorize and normalize the target of an event-triggered launch.

    Mirrors scheduled-task create: the caller must be able to start sessions
    with the agent, a pinned host must be theirs (and not an existing
    sandbox), a pinned workspace needs a host and is validated against the
    agent's boundary, and ``managed_sandbox`` needs a server that can
    provision sandboxes. Another user's agent is copied for the owner.

    :param execution_target: ``"connected_host"`` or ``"managed_sandbox"``.
    :param noun: What is being created, used in error messages, e.g.
        ``"ladder"``.
    :returns: ``(agent_id, workspace, model_override)`` to persist.
    """
    if execution_target not in SUBSCRIPTION_EXECUTION_TARGETS:
        raise OmnigentError(
            "execution_target must be 'connected_host' or 'managed_sandbox'",
            code=ErrorCode.INVALID_INPUT,
        )
    agent = await validate_session_agent(
        user_id=owner_id,
        agent_id=agent_id,
        agent_store=agent_store,
        permission_store=permission_store,
        conversation_store=conversation_store,
        egm_new_work=True,
    )
    normalized_model, _ = validate_session_model_metadata(
        model_override=model_override, reasoning_effort=None
    )
    normalized_workspace: str | None = None
    if execution_target == "managed_sandbox":
        if host_id is not None or workspace is not None:
            raise OmnigentError(
                f"a managed_sandbox {noun} runs in a fresh sandbox per event; "
                "do not set host_id or workspace",
                code=ErrorCode.INVALID_INPUT,
            )
        sandbox_config = getattr(request.app.state, "sandbox_config", None)
        if sandbox_config is None or not sandbox_config.managed_launch_supported:
            raise OmnigentError(
                "managed sandboxes are not configured on this server",
                code=ErrorCode.INVALID_INPUT,
            )
    else:
        host_store = getattr(request.app.state, "host_store", None)
        if host_id is not None and host_store is not None:
            host = await asyncio.to_thread(
                resolve_host_owner,
                user_id=owner_id,
                host_id=host_id,
                host_store=host_store,
            )
            if host.sandbox_provider is not None:
                raise OmnigentError(
                    f"{noun}s cannot use an existing sandbox; select a new sandbox for each run",
                    code=ErrorCode.INVALID_INPUT,
                )
        if workspace is not None:
            if host_id is None:
                raise OmnigentError(
                    "host_id required when workspace is set", code=ErrorCode.INVALID_INPUT
                )
            normalized_workspace = await validate_existing_host_workspace(
                user_id=owner_id,
                host_id=host_id,
                workspace=workspace,
                agent=agent,
                agent_cache=agent_cache,
                host_store=host_store,
                host_registry=getattr(request.app.state, "host_registry", None),
            )
    if agent.managed_by == "egm-template":
        from omnigent.server.egm import require_egm_target

        require_egm_target(host_id, normalized_workspace, execution_target)
        return agent.id, normalized_workspace, normalized_model
    bound = await asyncio.to_thread(agent_for_user, agent_store, artifact_store, agent, owner_id)
    return bound.id, normalized_workspace, normalized_model
