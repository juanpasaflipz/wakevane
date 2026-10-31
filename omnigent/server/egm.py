"""Packaged EGM availability and immutable run admission."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

import yaml

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.agent_store.egm import EGM_NAME
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

_logger = logging.getLogger(__name__)


def ensure_egm(store, artifacts, cache, *, enabled: bool) -> None:
    """Validate optional packaged content, then seed without taking over custom rows."""
    if not isinstance(store, SqlAlchemyAgentStore):
        return
    store.egm_bundle_location = None
    if not enabled or not store.supports_egm:
        return
    from omnigent.server.app import _POLLY_BUNDLE_SOURCE, _tar_gz_dir
    from omnigent.spec import load, materialize_bundle

    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = materialize_bundle(
                _POLLY_BUNDLE_SOURCE.parent / "egm", Path(tmp) / "bundle"
            )
            config = directory / "config.yaml"
            data = yaml.safe_load(config.read_text())
            if not isinstance(data, dict):
                raise ValueError("EGM config must be a mapping")
            data["name"] = EGM_NAME
            config.write_text(yaml.safe_dump(data, sort_keys=False))
            for path in directory.rglob("*.yaml"):
                if "${" in path.read_text():
                    raise ValueError("EGM cannot contain server environment placeholders")
            bundle = _tar_gz_dir(directory)
            load(
                bundle,
                dest=Path(tmp) / "validated",
                expand_env=False,
                enforce_handler_allowlist=True,
            )
    except (OSError, ValueError, OmnigentError, yaml.YAMLError):
        _logger.exception("Packaged Design review is missing or invalid; skipping EGM")
        return
    location = f"{builtin_agent_id(EGM_NAME)}/{hashlib.sha256(bundle).hexdigest()}"
    expected = store.get(builtin_agent_id(EGM_NAME))
    artifacts.put(location, bundle)
    row = store.seed_egm(location, expected)
    if row is None:
        _logger.warning("Design review identity is custom or collided; preserving it")
        return
    cache.evict(row.id)
    store.egm_bundle_location = location


def require_available(store, agent) -> None:
    if not isinstance(store, SqlAlchemyAgentStore) or not store.egm_available(agent):
        raise OmnigentError(
            "Design review is unavailable; reload the agent picker", code=ErrorCode.CONFLICT
        )


def snapshot_agent(store, artifacts, agent, owner):
    """Persist an immutable independently stored bundle; compensate failed row creation."""
    require_available(store, agent)
    if artifacts is None:
        raise OmnigentError(
            "Design review requires artifact storage", code=ErrorCode.INTERNAL_ERROR
        )
    bundle = artifacts.get(agent.bundle_location)
    if hashlib.sha256(bundle).hexdigest() != agent.bundle_location.rsplit("/", 1)[-1]:
        raise OmnigentError("Design review bundle digest mismatch", code=ErrorCode.CONFLICT)
    agent_id = generate_agent_id()
    location = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
    try:
        artifacts.put(location, bundle)
        return store.create_egm_snapshot(agent, agent_id, location, owner)
    except Exception:
        from omnigent.server.routes._sessions.helpers import (
            _delete_stored_session_bundle_after_failure,
        )

        _delete_stored_session_bundle_after_failure(artifacts, location)
        raise


async def validate_egm_output(
    host_registry,
    host_id: str | None,
    workspace: str | None,
    host_store=None,
    *,
    cloud_review: bool = False,
) -> None:
    """Check the pre-existing review directory on the chosen connected host.

    :param cloud_review: The session continues a cloud review, whose managed
        sandbox host was prepared and confinement-checked at launch.
    """
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        _ask_host_stat,
    )

    require_egm_target(host_id, workspace)
    assert host_id is not None and workspace is not None
    if host_store is not None:
        import asyncio

        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None or (host.sandbox_provider is not None) != cloud_review:
            raise OmnigentError(
                "Design review requires an external connected host"
                if not cloud_review
                else "Cloud Design review continues only on its own cloud workspace",
                code=ErrorCode.INVALID_INPUT,
            )
    connection = host_registry.get(host_id) if host_registry else None
    if connection is None:
        raise OmnigentError(
            "Design review requires a connected host", code=ErrorCode.INVALID_INPUT
        )
    try:
        stat = await _ask_host_stat(
            host_registry=host_registry,
            host_conn=connection,
            path=workspace.rstrip("/") + "/docs/designs/egm",
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc
    if not stat.get("exists") or stat.get("type") != "directory":
        raise OmnigentError(
            "Create docs/designs/egm in the selected repository before starting Design review",
            code=ErrorCode.INVALID_INPUT,
        )
    canonical = stat.get("canonical_path") or ""
    if not canonical or not canonical.startswith(workspace.rstrip("/") + "/"):
        raise OmnigentError(
            "Review output must remain inside the repository", code=ErrorCode.INVALID_INPUT
        )
    try:
        git = await _ask_host_stat(
            host_registry=host_registry, host_conn=connection, path=workspace.rstrip("/") + "/.git"
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc
    if not git.get("exists"):
        raise OmnigentError(
            "Select a Git repository for Design review", code=ErrorCode.INVALID_INPUT
        )


def require_egm_target(host_id, workspace, host_type=None) -> None:
    if not host_id or not workspace or host_type in {"managed", "managed_sandbox"}:
        raise OmnigentError(
            "Design review requires a connected host and repository workspace",
            code=ErrorCode.INVALID_INPUT,
        )


async def validate_continuation(
    agent, body, request, user_id, permissions, conversations, artifacts
):
    """Authorize a declared child using stored ancestry, never caller provenance."""
    import asyncio

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.auth import LEVEL_EDIT
    from omnigent.server.routes._auth_helpers import require_access
    from omnigent.util.session_lifecycle import is_session_closed

    if not body.parent_session_id or body.sub_agent_name != "goldfish":
        raise OmnigentError(
            "Only Goldfish continuation can reuse a Design review snapshot",
            code=ErrorCode.CONFLICT,
        )
    await require_access(user_id, body.parent_session_id, LEVEL_EDIT, permissions, conversations)
    if artifacts is None:
        raise OmnigentError("Snapshot storage is unavailable", code=ErrorCode.CONFLICT)
    bundle = await asyncio.to_thread(artifacts.get, agent.bundle_location)
    if hashlib.sha256(bundle).hexdigest() != agent.bundle_location.rsplit("/", 1)[-1]:
        raise OmnigentError("Snapshot digest mismatch", code=ErrorCode.CONFLICT)
    parent = await asyncio.to_thread(conversations.get_conversation, body.parent_session_id)
    current = parent
    seen = set()
    while current is not None:
        if (
            current.id in seen
            or current.agent_id != agent.id
            or is_session_closed(current.labels, current.title)
        ):
            raise OmnigentError(
                "Invalid or closed Design review ancestry", code=ErrorCode.CONFLICT
            )
        seen.add(current.id)
        if current.parent_conversation_id is None:
            break
        current = await asyncio.to_thread(
            conversations.get_conversation, current.parent_conversation_id
        )
    if current is None or parent is None:
        raise OmnigentError("Design review root is missing", code=ErrorCode.CONFLICT)
    await require_access(user_id, current.id, LEVEL_EDIT, permissions, conversations)
    token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
    if token and token_bound_runner_id(token) != parent.runner_id:
        raise OmnigentError("Runner is not bound to the parent review", code=ErrorCode.FORBIDDEN)
    if (body.host_id and body.host_id != parent.host_id) or (
        body.workspace and body.workspace != parent.workspace
    ):
        raise OmnigentError(
            "Review children inherit their parent's host and workspace",
            code=ErrorCode.INVALID_INPUT,
        )
    return body.model_copy(update={"host_id": parent.host_id, "workspace": parent.workspace})


def reject_egm_overrides(body) -> None:
    """Packaged runs cannot change the reviewed harness, policy or tool grants."""
    for field in (
        "harness_override",
        "cost_control_mode_override",
        "subagent_routing_override",
        "terminal_launch_args",
        "mcp_registry_services",
        "git",
    ):
        if getattr(body, field, None) not in (None, [], "off"):
            raise OmnigentError(
                f"Design review does not accept {field}", code=ErrorCode.INVALID_INPUT
            )


def current_template(store):
    template = store.get(builtin_agent_id(EGM_NAME))
    if template is None:
        raise OmnigentError("Design review is unavailable", code=ErrorCode.CONFLICT)
    require_available(store, template)
    return template


def cleanup_abandoned_snapshots(store, artifacts, *, before: int) -> int:
    """Offline maintenance: reclaim unreferenced snapshots with all writers stopped."""
    if not isinstance(store, SqlAlchemyAgentStore):
        return 0
    count = 0
    for agent in store.abandoned_egm_snapshots(before):
        # The operator runs this while writers are stopped, across split stores too.
        artifacts.delete(agent.bundle_location)
        store.delete(agent.id)
        count += 1
    return count


def discard_failed_snapshot(store, artifacts, agent) -> None:
    """Compensate creation without deleting a concurrently referenced run or masking errors."""
    from omnigent.server.routes._sessions.helpers import (
        _delete_stored_session_bundle_after_failure,
    )

    try:
        current = store.get(agent.id)
        if current is not None and current.session_id is None:
            store.delete(agent.id)
            _delete_stored_session_bundle_after_failure(artifacts, agent.bundle_location)
    except Exception:
        _logger.warning("Could not reclaim failed EGM snapshot %s", agent.id, exc_info=True)
