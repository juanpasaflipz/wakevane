"""REST CRUD for event subscriptions (``/v1/subscriptions``).

A subscription ties one session to external events of a ``(source, kind)``
whose fields satisfy a filter; the server's event bus posts a wake notice into
the session when one arrives. A *launch* subscription names an ``agent_id`` and
a ``prompt_template`` instead, and every matching event starts a fresh session
for its owner through the scheduled-task fire path. These endpoints let a
client (or, via the runner, an agent) create, list, read, update and delete
subscriptions.

Ownership mirrors scheduled tasks: rows are scoped to the calling user
(``"local"`` when auth is disabled) and a row owned by someone else 404s so
subscriptions are not enumerable across users. Creating one additionally
requires edit access to the target conversation, since the wake notice starts
a turn there; a launch subscription instead requires the same access to its
agent (and pinned host/workspace) that creating a scheduled task does.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import Subscription, SubscriptionState
from omnigent.entities.subscription import SUBSCRIPTION_EXECUTION_TARGETS, SUBSCRIPTION_SOURCES
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.bundles import agent_for_user
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.subscription_store import SubscriptionStore

# ``kind`` is a dotted lowercase identifier: ``push``, ``check_run.completed``.
KIND_PATTERN = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")
# Bound the filter so a subscription cannot carry an arbitrary document.
MAX_MATCH_KEYS = 16
MAX_NOTE_CHARS = 512
# Bound the launch prompt template like a scheduled task's prompt would be.
MAX_PROMPT_TEMPLATE_CHARS = 16_000
# Fields that only apply to a launch (``agent_id``) subscription.
_LAUNCH_FIELDS = (
    "prompt_template",
    "execution_target",
    "host_id",
    "workspace",
    "max_cost_usd",
    "model_override",
)
_SCALAR_TYPES = (str, int, float, bool)


def _validate_match(match: dict[str, Any] | None) -> dict[str, Any]:
    """
    Validate a ``match`` filter: at most :data:`MAX_MATCH_KEYS` scalar values.

    :param match: The caller-supplied filter, or ``None``.
    :returns: The filter (empty dict for ``None``).
    :raises ValueError: On too many keys, empty keys or non-scalar values.
    """
    if match is None:
        return {}
    if len(match) > MAX_MATCH_KEYS:
        raise ValueError(f"match may have at most {MAX_MATCH_KEYS} keys")
    for key, value in match.items():
        if not key:
            raise ValueError("match keys must be non-empty strings")
        if value is None or not isinstance(value, _SCALAR_TYPES):
            raise ValueError(f"match value for {key!r} must be a string, number or boolean")
    return match


class CreateSubscriptionRequest(BaseModel):
    """Body for ``POST /v1/subscriptions``."""

    model_config = ConfigDict(extra="forbid")

    # Exactly one of conversation_id (wake that session) and agent_id (launch
    # a new session per event) — checked in the handler so it answers 400.
    conversation_id: str | None = Field(default=None, min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    source: str
    kind: str
    match: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    # Launch-only fields, mirroring scheduled tasks.
    prompt_template: str | None = Field(default=None, max_length=MAX_PROMPT_TEMPLATE_CHARS)
    execution_target: str | None = None
    host_id: str | None = Field(default=None, min_length=1)
    workspace: str | None = Field(default=None, min_length=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    model_override: str | None = None

    @model_validator(mode="after")
    def _validate_create(self) -> CreateSubscriptionRequest:
        if self.source not in SUBSCRIPTION_SOURCES:
            raise ValueError(f"source must be one of {sorted(SUBSCRIPTION_SOURCES)}")
        if not KIND_PATTERN.fullmatch(self.kind):
            raise ValueError("kind must look like 'push' or 'check_run.completed'")
        self.match = _validate_match(self.match)
        return self


class UpdateSubscriptionRequest(BaseModel):
    """Body for ``PATCH /v1/subscriptions/{id}``. Unset fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    state: str | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    match: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateSubscriptionRequest:
        if self.state is not None and self.state not in {
            SubscriptionState.ACTIVE.value,
            SubscriptionState.PAUSED.value,
        }:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete")
        if self.match is not None:
            self.match = _validate_match(self.match)
        return self


def subscription_response(subscription: Subscription) -> dict[str, Any]:
    """Serialize a :class:`Subscription` to a JSON-safe dict."""
    return {
        "id": subscription.id,
        "owner_user_id": subscription.user_id,
        "conversation_id": subscription.conversation_id,
        "agent_id": subscription.agent_id,
        "prompt_template": subscription.prompt_template,
        "execution_target": subscription.execution_target,
        "host_id": subscription.host_id,
        "workspace": subscription.workspace,
        "max_cost_usd": subscription.max_cost_usd,
        "model_override": subscription.model_override,
        "source": subscription.source,
        "kind": subscription.kind,
        "match": subscription.match,
        "note": subscription.note,
        "state": subscription.state,
        "created_at": subscription.created_at,
        "updated_at": subscription.updated_at,
        "last_fired_at": subscription.last_fired_at,
        "fire_count": subscription.fire_count,
    }


def create_subscriptions_router(
    store: SubscriptionStore,
    *,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: Any | None = None,
    artifact_store: ArtifactStore | None = None,
) -> APIRouter:
    """Build the subscriptions router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/subscriptions[/{id}]``.

    :param store: The shared :class:`SubscriptionStore`.
    :param conversation_store: Resolves the target conversation on create.
    :param permission_store: Grants checked for conversation access; ``None``
        skips the check (single-user mode).
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` disables auth (owner resolves to ``"local"``).
    :param agent_store: Resolves a launch subscription's agent. ``None``
        rejects launch subscriptions with 400.
    :param agent_cache: Loads agent specs for pinned-workspace validation.
    :param artifact_store: Holds the copy a launch subscription takes of
        another user's agent, as for scheduled tasks.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def _validate_launch_target(
        request: Request, body: CreateSubscriptionRequest, owner_id: str | None
    ) -> tuple[str, str | None, str | None]:
        """Authorize and normalize a launch subscription's target.

        Mirrors scheduled-task create: the caller must be able to start
        sessions with the agent, a pinned host must be theirs (and not an
        existing sandbox), a pinned workspace needs a host and is validated
        against the agent's boundary, and ``managed_sandbox`` needs a server
        that can provision sandboxes. Another user's agent is copied for the
        owner, as for a scheduled task.

        :returns: ``(agent_id, workspace, model_override)`` to persist.
        """
        if agent_store is None:
            raise OmnigentError(
                "launch subscriptions are not available on this server",
                code=ErrorCode.INVALID_INPUT,
            )
        if not body.prompt_template or not body.prompt_template.strip():
            raise OmnigentError(
                "prompt_template is required with agent_id", code=ErrorCode.INVALID_INPUT
            )
        execution_target = body.execution_target or "connected_host"
        if execution_target not in SUBSCRIPTION_EXECUTION_TARGETS:
            raise OmnigentError(
                "execution_target must be 'connected_host' or 'managed_sandbox'",
                code=ErrorCode.INVALID_INPUT,
            )
        agent = await validate_session_agent(
            user_id=owner_id,
            agent_id=body.agent_id or "",
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
            egm_new_work=True,
        )
        model_override, _ = validate_session_model_metadata(
            model_override=body.model_override, reasoning_effort=None
        )
        workspace: str | None = None
        if execution_target == "managed_sandbox":
            if body.host_id is not None or body.workspace is not None:
                raise OmnigentError(
                    "a managed_sandbox subscription runs in a fresh sandbox per event; "
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
            if body.host_id is not None and host_store is not None:
                host = await asyncio.to_thread(
                    resolve_host_owner,
                    user_id=owner_id,
                    host_id=body.host_id,
                    host_store=host_store,
                )
                if host.sandbox_provider is not None:
                    raise OmnigentError(
                        "subscriptions cannot use an existing sandbox; "
                        "select a new sandbox for each run",
                        code=ErrorCode.INVALID_INPUT,
                    )
            if body.workspace is not None:
                if body.host_id is None:
                    raise OmnigentError(
                        "host_id required when workspace is set", code=ErrorCode.INVALID_INPUT
                    )
                workspace = await validate_existing_host_workspace(
                    user_id=owner_id,
                    host_id=body.host_id,
                    workspace=body.workspace,
                    agent=agent,
                    agent_cache=agent_cache,
                    host_store=host_store,
                    host_registry=getattr(request.app.state, "host_registry", None),
                )
        if agent.managed_by == "egm-template":
            from omnigent.server.egm import require_egm_target

            require_egm_target(body.host_id, workspace, execution_target)
            return agent.id, workspace, model_override
        bound = await asyncio.to_thread(
            agent_for_user, agent_store, artifact_store, agent, owner_id
        )
        return bound.id, workspace, model_override

    def _owner(request: Request) -> str:
        """Resolve the calling user, mapping the auth-disabled case to
        ``RESERVED_USER_LOCAL`` so single-user rows are always owned."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _require_owned(subscription_id: str, owner_id: str | None) -> Subscription:
        """Load a subscription the caller owns, or raise 404."""
        subscription = store.get(subscription_id)
        if (
            subscription is None
            or subscription.state == SubscriptionState.DELETED.value
            or subscription.user_id != owner_id
        ):
            raise OmnigentError("Subscription not found", code=ErrorCode.NOT_FOUND)
        return subscription

    @router.post("/subscriptions")
    async def create_subscription(
        request: Request,
        body: CreateSubscriptionRequest,
    ) -> dict[str, Any]:
        """Create a wake subscription (conversation) or a launch subscription (agent)."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        if (body.conversation_id is None) == (body.agent_id is None):
            raise OmnigentError(
                "exactly one of conversation_id and agent_id must be set",
                code=ErrorCode.INVALID_INPUT,
            )
        if body.agent_id is not None:
            agent_id, workspace, model_override = await _validate_launch_target(
                request, body, owner_id
            )
            subscription = store.create(
                uuid.uuid4().hex,
                user_id=owner_id,
                agent_id=agent_id,
                source=body.source,
                kind=body.kind,
                match=body.match,
                note=body.note,
                prompt_template=body.prompt_template,
                execution_target=body.execution_target or "connected_host",
                host_id=body.host_id,
                workspace=workspace,
                max_cost_usd=body.max_cost_usd,
                model_override=model_override,
            )
            return subscription_response(subscription)

        conversation_id = body.conversation_id
        assert conversation_id is not None  # exactly-one-target check above
        launch_only = [name for name in _LAUNCH_FIELDS if getattr(body, name) is not None]
        if launch_only:
            raise OmnigentError(
                f"{', '.join(launch_only)} only apply to a subscription with agent_id",
                code=ErrorCode.INVALID_INPUT,
            )
        # Wake notices start turns in the target session, so the caller needs
        # edit access there (same bar as posting a message).
        await require_access(
            owner_id, conversation_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if (
            permission_store is None
            and conversation_store.get_conversation(conversation_id) is None
        ):
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        subscription = store.create(
            uuid.uuid4().hex,
            user_id=owner_id,
            conversation_id=conversation_id,
            source=body.source,
            kind=body.kind,
            match=body.match,
            note=body.note,
        )
        return subscription_response(subscription)

    @router.get("/subscriptions")
    async def list_subscriptions(request: Request) -> dict[str, list[dict[str, Any]]]:
        """List the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        return {"subscriptions": [subscription_response(s) for s in store.list_for_user(owner_id)]}

    @router.get("/subscriptions/{subscription_id}")
    async def get_subscription(request: Request, subscription_id: str) -> dict[str, Any]:
        """Fetch one of the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        return subscription_response(_require_owned(subscription_id, owner_id))

    @router.patch("/subscriptions/{subscription_id}")
    async def update_subscription(
        request: Request,
        subscription_id: str,
        body: UpdateSubscriptionRequest,
    ) -> dict[str, Any]:
        """Pause/resume a subscription or change its note / filter."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(subscription_id, owner_id)
        fields = body.model_dump(exclude_unset=True)
        kwargs: dict[str, Any] = {}
        if "state" in fields:
            kwargs["state"] = fields["state"]
        if "note" in fields:
            kwargs["note"] = fields["note"]
        if "match" in fields and fields["match"] is not None:
            kwargs["match"] = fields["match"]
        updated = store.update(subscription_id, **kwargs)
        if updated is None:
            raise OmnigentError("Subscription not found", code=ErrorCode.NOT_FOUND)
        return subscription_response(updated)

    @router.delete("/subscriptions/{subscription_id}")
    async def delete_subscription(request: Request, subscription_id: str) -> dict[str, Any]:
        """Soft-delete one of the caller's subscriptions."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(subscription_id, owner_id)
        store.delete(subscription_id)
        return {"deleted": True, "id": subscription_id}

    return router
