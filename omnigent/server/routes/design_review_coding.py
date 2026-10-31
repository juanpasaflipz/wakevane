"""Approve a cloud Design review specification, start its coding session, open its PR.

Approval and pull requests are owner actions on the review's root session and
require the ``egm_cloud_coding`` feature. Reading approvals and stored coding
evidence follows the review session's read permission. Nothing here merges or
deploys.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.egm_coding import (
    PULL_REQUEST_CAPABILITY,
    CodingBinding,
    PullRequestError,
    approval_lock,
    approved_spec,
    coding_binding,
    coding_instructions,
    coding_labels,
    list_approvals,
    new_approval_record,
    open_pull_request,
    publish_coding_outputs,
    read_approval,
    read_coding_blob,
    read_coding_manifest,
    save_approval,
)
from omnigent.server.egm_outputs import OutputPublicationError
from omnigent.server.feature_flags import Feature
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


class DesignApprovalRequest(BaseModel):
    """The saved specification the owner approves for implementation."""

    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    agent_id: str | None = None
    sandbox_provider: str | None = None


def create_design_review_coding_router(
    *,
    artifact_store: ArtifactStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Build the ``/sessions/{id}/design-review/approve|approvals`` routes."""
    router = APIRouter()

    def _require_enabled(request: Request) -> None:
        flags = getattr(request.app.state, "feature_flags", None)
        if flags is None or not flags.enabled(Feature.EGM_CLOUD_CODING):
            raise OmnigentError(
                "Approved coding is not enabled on this server", code=ErrorCode.NOT_FOUND
            )

    async def _review_root(
        request: Request, session_id: str, level: int
    ) -> tuple[str | None, Any]:
        user_id = require_user(request, auth_provider)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        root_id = conversation.root_conversation_id or conversation.id
        await require_access(user_id, root_id, level, permission_store, conversation_store)
        root = (
            conversation
            if root_id == conversation.id
            else await asyncio.to_thread(conversation_store.get_conversation, root_id)
        )
        if root is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return user_id, root

    def _record(root_id: str, approval_id: str) -> dict[str, Any]:
        record = read_approval(artifact_store, root_id, approval_id)
        if record is None:
            raise OmnigentError("Approval not found", code=ErrorCode.NOT_FOUND)
        return record

    async def _coding_agent_id(requested: str | None) -> str:
        from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME

        if requested is not None:
            agent = await asyncio.to_thread(agent_store.get, requested)
        else:
            agent = await asyncio.to_thread(agent_store.get_by_name, CLAUDE_NATIVE_AGENT_NAME)
        if agent is None:
            raise OmnigentError("The coding agent is not available", code=ErrorCode.NOT_FOUND)
        if agent.managed_by in {"egm-template", "egm-session"}:
            raise OmnigentError(
                "Design review agents cannot implement an approval", code=ErrorCode.INVALID_INPUT
            )
        return agent.id

    async def _start_coding_session(
        request: Request,
        record: dict[str, Any],
        spec: bytes,
        user_id: str | None,
        body: DesignApprovalRequest,
        workspace: str,
        provider: str | None,
    ) -> str:
        from omnigent.server.schemas import SessionCreateRequest

        create = getattr(request.app.state, "create_json_session", None)
        if create is None:
            raise OmnigentError(
                "Session creation is unavailable on this server", code=ErrorCode.INTERNAL_ERROR
            )
        create_body = SessionCreateRequest.model_validate(
            {
                "agent_id": await _coding_agent_id(body.agent_id),
                "host_type": "managed",
                "sandbox_provider": provider,
                "workspace": workspace,
                "title": f"Implement {record['spec_path']}",
                "initial_input": {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": coding_instructions(record, spec)}
                        ],
                    },
                },
            }
        )
        response = await create(request, create_body, user_id, server_labels=coding_labels(record))
        return response.id

    async def _review_launch_target(
        request: Request, root: Any, requested_provider: str | None
    ) -> tuple[str, str | None, str | None, str]:
        """Repository workspace, base branch, sandbox provider and funding mode."""
        from omnigent.server.egm_cloud import REPOSITORY_LABEL
        from omnigent.server.funded_trial import funded_target
        from omnigent.server.managed_hosts import (
            parse_repo_workspace,
            read_managed_repo_workspaces,
        )

        recorded = read_managed_repo_workspaces(root.labels)
        workspace = recorded[0] if recorded else root.labels[REPOSITORY_LABEL]
        try:
            base_branch = parse_repo_workspace(workspace).branch
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        provider = requested_provider
        host_store = getattr(request.app.state, "host_store", None)
        if provider is None and host_store is not None and root.host_id is not None:
            host = await asyncio.to_thread(host_store.get_host, root.host_id)
            provider = host.sandbox_provider if host is not None else None
        sandbox_config = getattr(request.app.state, "sandbox_config", None)
        funding = "trial" if funded_target(sandbox_config, provider) is not None else "deployment"
        return workspace, base_branch, provider, funding

    @router.post(
        "/sessions/{session_id}/design-review/approve",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def approve_design(
        request: Request, session_id: str, body: DesignApprovalRequest
    ) -> dict[str, Any]:
        """Approve one saved specification and start its separate coding session.

        Idempotent per review, spec path, hash and commit: a repeat returns the
        existing approval and its coding session instead of starting another.
        """
        _require_enabled(request)
        user_id, root = await _review_root(request, session_id, LEVEL_OWNER)
        spec, manifest = await asyncio.to_thread(
            approved_spec,
            artifact_store,
            root.id,
            root.labels,
            path=body.path,
            sha256=body.sha256,
            commit=body.commit,
        )
        workspace, base_branch, provider, funding = await _review_launch_target(
            request, root, body.sandbox_provider
        )
        record = new_approval_record(
            review_session_id=root.id,
            labels=root.labels,
            path=body.path,
            sha256=body.sha256,
            size=len(spec),
            manifest=manifest,
            approved_by=user_id,
            base_branch=base_branch,
            funding=funding,
        )
        async with approval_lock(record["id"]):
            existing = await asyncio.to_thread(
                read_approval, artifact_store, root.id, record["id"]
            )
            if existing is not None and existing.get("coding_session_id"):
                return {"approval": existing, "created": False}
            record = existing or record
            record["coding_error"] = None
            await asyncio.to_thread(save_approval, artifact_store, record)
            try:
                record["coding_session_id"] = await _start_coding_session(
                    request, record, spec, user_id, body, workspace, provider
                )
            except OmnigentError as exc:
                record["coding_error"] = exc.message
                await asyncio.to_thread(save_approval, artifact_store, record)
                raise
            await asyncio.to_thread(save_approval, artifact_store, record)
        return {"approval": record, "created": True}

    @router.get("/sessions/{session_id}/design-review/approvals")
    async def get_approvals(
        request: Request, response: Response, session_id: str
    ) -> dict[str, Any]:
        """Approvals for a review, each with its latest stored coding outputs."""
        _, root = await _review_root(request, session_id, LEVEL_READ)
        approvals = await asyncio.to_thread(list_approvals, artifact_store, root.id)
        for record in approvals:
            record["coding_outputs"] = await asyncio.to_thread(
                read_coding_manifest, artifact_store, root.id, record["id"]
            )
        response.headers["Cache-Control"] = "private, no-store"
        return {"approvals": approvals}

    @router.get("/sessions/{session_id}/design-review/approvals/{approval_id}/{kind}")
    async def get_coding_evidence(
        request: Request,
        session_id: str,
        approval_id: str,
        kind: Literal["patch", "test-output"],
    ) -> Response:
        """Download the stored patch or test output of an approval's coding session."""
        _, root = await _review_root(request, session_id, LEVEL_READ)
        manifest = await asyncio.to_thread(
            read_coding_manifest, artifact_store, root.id, approval_id
        )
        entry = (manifest or {}).get("patch" if kind == "patch" else "tests")
        if not entry:
            raise OmnigentError("No coding evidence has been saved yet", code=ErrorCode.NOT_FOUND)
        try:
            data = await asyncio.to_thread(
                read_coding_blob, artifact_store, root.id, approval_id, entry["sha256"]
            )
        except OutputPublicationError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INTERNAL_ERROR) from exc
        name = f"{approval_id}.{'patch' if kind == 'patch' else 'test-output.txt'}"
        return Response(
            content=data,
            media_type="text/plain; charset=utf-8",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-SHA256": entry["sha256"],
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _refresh_coding_outputs(request: Request, record: dict[str, Any]) -> None:
        """Best effort: capture the live coding sandbox's latest changes first."""
        from omnigent.server.managed_hosts import _launcher_for_teardown

        session = await asyncio.to_thread(
            conversation_store.get_conversation, record["coding_session_id"]
        )
        binding: CodingBinding | None = coding_binding(session.labels) if session else None
        host_store = getattr(request.app.state, "host_store", None)
        if binding is None or session is None or host_store is None or not session.host_id:
            return
        host = await asyncio.to_thread(host_store.get_host, session.host_id)
        if host is None or host.sandbox_id is None or host.deleted_at is not None:
            return
        launcher = _launcher_for_teardown(host, getattr(request.app.state, "sandbox_config", None))
        if launcher is None or not session.workspace:
            return
        try:
            await asyncio.to_thread(
                publish_coding_outputs,
                launcher,
                host.sandbox_id,
                store=artifact_store,
                binding=binding,
                session_id=session.id,
                workspace=session.workspace,
                reason="pull_request",
            )
        except Exception:
            _logger.warning(
                "Could not refresh coding outputs for %s; using the last checkpoint",
                session.id,
                exc_info=True,
            )

    @router.post(
        "/sessions/{session_id}/design-review/approvals/{approval_id}/pull-request",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def open_design_pull_request(
        request: Request, session_id: str, approval_id: str
    ) -> dict[str, Any]:
        """Push the stored coding changes to a new branch and open a pull request.

        Uses the owner's GitHub connection. Never merges or deploys.
        """
        from omnigent.server.funded_trial_store import FundedTrialDenied
        from omnigent.server.github_app_client import GitHubApiError
        from omnigent.server.github_identity import resolve_access_token

        _require_enabled(request)
        user_id, root = await _review_root(request, session_id, LEVEL_OWNER)
        record = await asyncio.to_thread(_record, root.id, approval_id)
        if not record.get("coding_session_id"):
            raise OmnigentError("This approval has no coding session yet", code=ErrorCode.CONFLICT)
        if record.get("funding") == "trial":
            policy = getattr(request.app.state, "funded_trial", None)
            try:
                if policy is None:
                    raise FundedTrialDenied("disabled", "The funded trial is not enabled.")
                await asyncio.to_thread(policy.active_grant, user_id, PULL_REQUEST_CAPABILITY)
            except FundedTrialDenied as denied:
                raise OmnigentError(denied.message, code=ErrorCode.FORBIDDEN) from None
        github_store = getattr(request.app.state, "github_store", None)
        github_client = getattr(request.app.state, "github_client", None)
        if github_store is None or github_client is None:
            raise OmnigentError(
                "GitHub is not connected on this server, so a pull request cannot be opened",
                code=ErrorCode.INVALID_INPUT,
            )
        account = user_id if user_id is not None else RESERVED_USER_LOCAL
        token = await resolve_access_token(account, store=github_store, client=github_client)
        connection = await asyncio.to_thread(github_store.get, account)
        if token is None or connection is None:
            raise OmnigentError(
                "Connect your GitHub account with write access to this repository first",
                code=ErrorCode.INVALID_INPUT,
            )
        async with approval_lock(approval_id):
            record = await asyncio.to_thread(_record, root.id, approval_id)
            if record.get("pull_request"):
                return {"approval": record, "created": False}
            await _refresh_coding_outputs(request, record)
            manifest = await asyncio.to_thread(
                read_coding_manifest, artifact_store, root.id, approval_id
            )
            if manifest is None:
                raise OmnigentError(
                    "The coding session has not saved any changes yet", code=ErrorCode.CONFLICT
                )
            login = connection.github_login
            author = {
                "name": login,
                "email": f"{connection.github_user_id}+{login}@users.noreply.github.com",
            }
            try:
                pull = await open_pull_request(
                    github_client,
                    token,
                    store=artifact_store,
                    record=record,
                    manifest=manifest,
                    author=author,
                )
            except PullRequestError as exc:
                raise OmnigentError(
                    str(exc),
                    code=ErrorCode.UPSTREAM_PERMISSION_DENIED
                    if exc.access_denied
                    else ErrorCode.CONFLICT,
                ) from exc
            except GitHubApiError as exc:
                raise OmnigentError(
                    f"GitHub could not open the pull request ({exc.status_code})",
                    code=ErrorCode.INTERNAL_ERROR,
                ) from exc
            record["pull_request"] = {**pull, "opened_by": user_id}
            await asyncio.to_thread(save_approval, artifact_store, record)
        return {"approval": record, "created": True}

    return router
