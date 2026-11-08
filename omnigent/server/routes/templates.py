"""One-click templates (``/v1/templates``).

``POST /v1/templates/factory`` starts a software factory on one of the
caller's GitHub repositories: it connects the repository's webhook, installs
the factory foreman as the caller's own agent with their budget and merge
gate, and creates the launch subscriptions that start a foreman session for
each new issue and each failed check. See :mod:`omnigent.server.factory_template`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.utils import installed_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.factory_template import (
    DEFAULT_DAILY_BUDGET_USD,
    MAX_DAILY_BUDGET_USD,
    build_factory_bundle,
    ensure_factory_subscriptions,
    factory_plan,
)
from omnigent.server.github_webhooks import connect_repository
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.builtin_agents import install_user_agent
from omnigent.server.routes.subscriptions import subscription_response
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.subscription_store import SubscriptionStore

_MAX_GITHUB_NAME_CHARS = 100


class FactoryTemplateRequest(BaseModel):
    """Body for ``POST /v1/templates/factory``."""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)
    repo: str = Field(min_length=1, max_length=_MAX_GITHUB_NAME_CHARS)
    daily_budget_usd: float = Field(
        default=DEFAULT_DAILY_BUDGET_USD, gt=0, le=MAX_DAILY_BUDGET_USD
    )
    ask_before_merge: bool = True


def create_templates_router(
    *,
    subscription_store: SubscriptionStore,
    agent_store: AgentStore,
    agent_cache: Any,
    artifact_store: ArtifactStore | None = None,
    auth_provider: AuthProvider | None = None,
    github_store: Any | None = None,
    github_client: Any | None = None,
) -> APIRouter:
    """Build the templates router, mounted with ``prefix="/v1"``.

    :param subscription_store: Holds the launch subscriptions the template creates.
    :param agent_store: Stores the installed foreman (needs user-agent support).
    :param agent_cache: Evicted when a repeated call changes the foreman.
    :param artifact_store: Holds the foreman's bundle; ``None`` answers 409.
    :param auth_provider: Identifies the caller; ``None`` disables auth.
    :param github_store: Per-user GitHub connections for the webhook step.
    :param github_client: GitHub App client for the webhook step.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/templates/factory")
    async def start_software_factory(
        request: Request, body: FactoryTemplateRequest
    ) -> dict[str, Any]:
        """Start (or return the existing) software factory for ``owner/repo``.

        Idempotent per caller and repository: a repeated call reuses the
        foreman agent (updating its budget and merge gate if they changed)
        and the subscriptions it already created.
        """
        user_id = require_user(request, auth_provider)
        owner_id = None if user_id in (None, RESERVED_USER_LOCAL) else user_id
        if artifact_store is None or not agent_store.supports_user_agents:
            raise OmnigentError(
                "this server cannot install custom agents, so the factory template is unavailable",
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
        plan = factory_plan(
            body.owner,
            body.repo,
            budget_usd=body.daily_budget_usd,
            ask_before_merge=body.ask_before_merge,
            managed_sandbox=execution_target == "managed_sandbox",
        )
        bundle = await asyncio.to_thread(build_factory_bundle, plan)
        await asyncio.to_thread(validate_agent_bundle, bundle, enforce_handler_allowlist=True)
        agent_existed = (
            await asyncio.to_thread(agent_store.get, installed_agent_id(user_id, plan.agent_name))
            is not None
        )
        agent = await asyncio.to_thread(
            install_user_agent,
            agent_store,
            artifact_store,
            agent_cache,
            owner=user_id,
            name=plan.agent_name,
            bundle_bytes=bundle,
        )
        subscriptions = await asyncio.to_thread(
            ensure_factory_subscriptions,
            subscription_store,
            plan,
            owner_id=owner_id,
            agent_id=agent.id,
            execution_target=execution_target,
        )
        return {
            "repo": plan.full_name,
            "agent_id": agent.id,
            "agent_name": agent.name,
            "agent_created": not agent_existed,
            "subscription_ids": [subscription.id for subscription, _ in subscriptions],
            "subscriptions": [
                {**subscription_response(subscription), "created": created}
                for subscription, created in subscriptions
            ],
            "hook": hook.to_response(),
        }

    return router
