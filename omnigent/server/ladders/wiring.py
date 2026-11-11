"""Attach ladders to a server app: store, engine, bus listener and routes."""

from __future__ import annotations

from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.policies.builder import load_session_usage_and_tree
from omnigent.server.auth import AuthProvider
from omnigent.server.events.bus import EventBus
from omnigent.server.ladders.engine import LadderEngine, SessionSpend, SpendReader
from omnigent.server.ladders.model import LadderAttempt
from omnigent.server.ladders.store import LadderStore
from omnigent.server.routes.ladders import create_ladders_router
from omnigent.server.scheduled.fire import LaunchOutcome, LaunchRequest, launch_session
from omnigent.server.usage_efficiency import model_counters_from_usage, session_cost_usd
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore


def build_spend_reader(conversation_store: ConversationStore) -> SpendReader:
    """
    Build the reader that prices an attempt from its session tree.

    :param conversation_store: Store holding the attempt's session.
    :returns: A reader yielding the session's cost and its top-spending model.
    """

    def read_spend(attempt: LadderAttempt) -> SessionSpend | None:
        if attempt.conversation_id is None:
            return None
        # The engine runs outside any request, so read in the attempt's tenant.
        with workspace_scope(attempt.workspace_id):
            if conversation_store.get_conversation(attempt.conversation_id) is None:
                return None
            usage, _tree = load_session_usage_and_tree(attempt.conversation_id, conversation_store)
        by_model = model_counters_from_usage(usage)
        top_model = max(by_model, key=lambda name: by_model[name].cost_usd, default=None)
        priced = "total_cost_usd" in usage
        return SessionSpend(cost_usd=session_cost_usd(usage) if priced else None, model=top_model)

    return read_spend


def mount_ladders(
    app: Any,
    event_bus: EventBus,
    *,
    storage_location: str,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
    agent_cache: Any | None,
    artifact_store: ArtifactStore | None,
) -> LadderEngine:
    """
    Start serving ladders on ``app``.

    The fire dependencies are looked up on ``app.state`` at launch time,
    because the server lifespan builds them after routes are mounted.

    :param storage_location: SQLAlchemy database URI the ladder tables live in.
    :returns: The engine, also kept on ``app.state.ladder_engine``.
    """
    store = LadderStore(storage_location)
    read_spend = build_spend_reader(conversation_store)

    async def launch(request: LaunchRequest) -> LaunchOutcome:
        deps = getattr(app.state, "scheduled_fire_deps", None)
        if deps is None:
            return LaunchOutcome(
                status="failed", error="the scheduled-task fire path is not configured"
            )
        return await launch_session(deps, request)

    engine = LadderEngine(store, launch, read_spend=read_spend)
    event_bus.add_listener(engine.handle_event)
    app.state.ladder_store = store
    app.state.ladder_engine = engine
    app.include_router(
        create_ladders_router(
            store,
            engine,
            conversation_store=conversation_store,
            agent_store=agent_store,
            permission_store=permission_store,
            auth_provider=auth_provider,
            agent_cache=agent_cache,
            artifact_store=artifact_store,
            read_spend=read_spend,
        ),
        prefix="/v1",
        tags=["ladders"],
    )
    return engine
