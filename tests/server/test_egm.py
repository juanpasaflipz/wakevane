"""Protected run continuation and negative admission paths for Design review."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from omnigent.db.utils import builtin_agent_id
from omnigent.errors import OmnigentError
from omnigent.server.egm import (
    ensure_egm,
    reject_egm_overrides,
    require_egm_target,
    snapshot_agent,
    validate_continuation,
)
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.routes.test_workspace_validation import host_setup as host_setup


@pytest.mark.asyncio
async def test_snapshot_continuation_after_disable(
    agent_store, artifact_store, conversation_store, db_uri
):
    ensure_egm(agent_store, artifact_store, Mock(), enabled=True)
    template = agent_store.get(builtin_agent_id("egm-review"))
    snapshot = snapshot_agent(agent_store, artifact_store, template, "owner")
    root = conversation_store.create_conversation(
        agent_id=snapshot.id, host_id="ab" * 16, workspace="/repo"
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in ("owner", "reader"):
        permissions.ensure_user(user)
    permissions.grant("owner", root.id, 4)
    permissions.grant("reader", root.id, 1)
    ensure_egm(agent_store, artifact_store, Mock(), enabled=False)
    request = Request(
        {"type": "http", "headers": [], "app": SimpleNamespace(state=SimpleNamespace())}
    )
    body = SessionCreateRequest(
        agent_id=snapshot.id, parent_session_id=root.id, sub_agent_name="goldfish"
    )
    approved = await validate_continuation(
        snapshot, body, request, "owner", permissions, conversation_store, artifact_store
    )
    assert approved.host_id == root.host_id and approved.workspace == "/repo"
    with pytest.raises(OmnigentError):
        await validate_continuation(
            snapshot, body, request, "reader", permissions, conversation_store, artifact_store
        )
    with pytest.raises(OmnigentError, match="snapshots cannot start roots"):
        await validate_session_agent(
            user_id="owner",
            agent_id=snapshot.id,
            agent_store=agent_store,
            permission_store=permissions,
            conversation_store=conversation_store,
        )
    for change in (
        {"sub_agent_name": None},
        {"host_id": "cd" * 16},
        {"workspace": "/other"},
        {"parent_session_id": "ef" * 16},
    ):
        with pytest.raises(OmnigentError):
            await validate_continuation(
                snapshot,
                body.model_copy(update=change),
                request,
                "owner",
                permissions,
                conversation_store,
                artifact_store,
            )
    conversation_store.set_labels(root.id, {"omnigent.closed": "true"})
    with pytest.raises(OmnigentError, match="closed"):
        await validate_continuation(
            snapshot, body, request, "owner", permissions, conversation_store, artifact_store
        )


@pytest.mark.parametrize(
    "host,workspace,kind",
    [(None, "/repo", None), ("host", None, None), ("host", "/repo", "managed")],
)
def test_egm_rejects_unsupported_target(host, workspace, kind):
    with pytest.raises(OmnigentError, match="connected host"):
        require_egm_target(host, workspace, kind)


def test_egm_default_request_and_override_rejection():
    body = SessionCreateRequest(agent_id="ab" * 16)
    reject_egm_overrides(body)
    for field, value in (
        ("harness_override", "codex"),
        ("mcp_registry_services", ["service"]),
        ("terminal_launch_args", ["--dangerously-skip-permissions"]),
    ):
        with pytest.raises(OmnigentError, match=field):
            reject_egm_overrides(body.model_copy(update={field: value}))


def test_snapshot_row_failure_removes_only_new_artifact(agent_store, artifact_store, monkeypatch):
    ensure_egm(agent_store, artifact_store, Mock(), enabled=True)
    template = agent_store.get(builtin_agent_id("egm-review"))
    original = artifact_store.get(template.bundle_location)

    def fail(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(agent_store, "create_egm_snapshot", fail)
    delete = Mock(wraps=artifact_store.delete)
    monkeypatch.setattr(artifact_store, "delete", delete)
    with pytest.raises(RuntimeError, match="database unavailable"):
        snapshot_agent(agent_store, artifact_store, template, "owner")
    delete.assert_called_once()
    assert delete.call_args.args[0] != template.bundle_location
    assert (
        hashlib.sha256(artifact_store.get(template.bundle_location)).digest()
        == hashlib.sha256(original).digest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host_setup", ["ab" * 16], indirect=True)
async def test_real_create_pins_root_and_continues_child_when_disabled(
    agent_store, artifact_store, conversation_store, db_uri, tmp_path, runtime_init, host_setup
):
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.routes._sessions.orchestration import _create_session_from_existing_agent
    from tests.server.routes.test_workspace_validation import _set_stat

    host_id = "ab" * 16

    registry, _, _ = host_setup
    for path in ("/repo", "/repo/docs/designs/egm", "/repo/.git"):
        _set_stat(registry, path)
    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "egm-cache")
    ensure_egm(agent_store, artifact_store, cache, enabled=True)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("owner")
    request = Request(
        {
            "type": "http",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(host_registry=registry, host_store=None)),
        }
    )
    body = SessionCreateRequest(
        agent_id=builtin_agent_id("egm-review"), host_id=host_id, workspace="/repo"
    )
    _, root = await _create_session_from_existing_agent(
        conversation_store,
        agent_store,
        None,
        body,
        request,
        agent_cache=cache,
        user_id="owner",
        permission_store=permissions,
        artifact_store=artifact_store,
    )
    permissions.grant("owner", root.id, 4)
    snapshot = agent_store.get(root.agent_id)
    assert snapshot.managed_by == "egm-session"
    assert root.agent_id != body.agent_id and root.workspace == "/repo"
    ensure_egm(agent_store, artifact_store, cache, enabled=False)
    child_body = SessionCreateRequest(
        agent_id=root.agent_id,
        parent_session_id=root.id,
        sub_agent_name="goldfish",
        title="fresh-critic",
    )
    _, child = await _create_session_from_existing_agent(
        conversation_store,
        agent_store,
        None,
        child_body,
        request,
        agent_cache=cache,
        user_id="owner",
        permission_store=permissions,
        artifact_store=artifact_store,
    )
    assert child.agent_id == root.agent_id
    assert child.workspace == root.workspace and child.host_id == root.host_id
    assert child.sub_agent_name == "goldfish"
    with pytest.raises(OmnigentError, match="unavailable"):
        await _create_session_from_existing_agent(
            conversation_store,
            agent_store,
            None,
            body,
            request,
            agent_cache=cache,
            user_id="owner",
            permission_store=permissions,
            artifact_store=artifact_store,
        )


@pytest.mark.asyncio
async def test_each_scheduled_firing_gets_a_fresh_snapshot(
    agent_store, artifact_store, conversation_store, tmp_path
):
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.scheduled.fire import FireDeps, _create_session
    from tests.server.scheduled.test_fire import _task

    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    ensure_egm(agent_store, artifact_store, cache, enabled=True)
    task = _task(agent_id=builtin_agent_id("egm-review"), host_id="ab" * 16)
    deps = FireDeps(
        None,
        agent_store,
        conversation_store,
        None,
        None,
        None,
        agent_cache=cache,
        artifact_store=artifact_store,
    )
    first = await _create_session(deps, task)
    second = await _create_session(deps, task)
    assert first.agent_id != second.agent_id and first.agent_id != task.agent_id
    assert agent_store.get(first.agent_id).managed_by == "egm-session"
    assert first.labels["omnigent.egm.snapshot"] == "true"
    ensure_egm(agent_store, artifact_store, cache, enabled=False)
    with pytest.raises(OmnigentError, match="unavailable"):
        await _create_session(deps, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("host_setup", ["ab" * 16], indirect=True)
async def test_fork_review_uses_current_template_and_rejects_disabled(
    agent_store, artifact_store, conversation_store, db_uri, tmp_path, runtime_init, host_setup
):
    import httpx

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from tests.server.routes.test_workspace_validation import _set_stat

    registry, _, _ = host_setup
    for path in ("/repo", "/repo/docs/designs/egm", "/repo/.git"):
        _set_stat(registry, path)
    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    ensure_egm(agent_store, artifact_store, cache, enabled=True)
    template = agent_store.get(builtin_agent_id("egm-review"))
    snapshot = snapshot_agent(agent_store, artifact_store, template, "owner")
    root = conversation_store.create_conversation(
        agent_id=snapshot.id, host_id="ab" * 16, workspace="/repo"
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("owner")
    permissions.grant("owner", root.id, 4)
    app = create_app(
        agent_store=agent_store,
        artifact_store=artifact_store,
        agent_cache=cache,
        conversation_store=conversation_store,
        file_store=SqlAlchemyFileStore(db_uri),
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    app.state.host_registry = registry
    app.state.host_store = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{root.id}/fork",
            headers={"X-Forwarded-Email": "owner"},
            json={"title": "new review"},
        )
        assert response.status_code in (200, 201), response.text
        fork = conversation_store.get_conversation(response.json()["id"])
        assert fork.agent_id not in (root.agent_id, template.id)
        assert agent_store.get(fork.agent_id).managed_by == "egm-session"
        assert fork.workspace == "/repo" and fork.host_id == root.host_id
        ensure_egm(agent_store, artifact_store, cache, enabled=False)
        response = await client.post(
            f"/v1/sessions/{root.id}/fork", headers={"X-Forwarded-Email": "owner"}, json={}
        )
        assert response.status_code == 409, response.text
