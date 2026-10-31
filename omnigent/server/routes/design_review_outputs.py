"""Read routes for durable cloud Design review outputs.

Access follows the review session's own permissions: anyone who can read the
session (or any session in its tree) can read its published handoff, and
revoking session access revokes output access.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.egm_outputs import OutputPublicationError, read_manifest, read_output
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def create_design_review_outputs_router(
    *,
    artifact_store: ArtifactStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Build the ``/sessions/{id}/design-review/outputs`` routes."""
    router = APIRouter()

    async def _root_id(request: Request, session_id: str) -> str:
        user_id = require_user(request, auth_provider)
        await require_access(user_id, session_id, LEVEL_READ, permission_store, conversation_store)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return conversation.root_conversation_id or conversation.id

    @router.get("/sessions/{session_id}/design-review/outputs")
    async def get_outputs(request: Request, response: Response, session_id: str) -> dict[str, Any]:
        root_id = await _root_id(request, session_id)
        manifest = await asyncio.to_thread(read_manifest, artifact_store, root_id)
        if manifest is None:
            raise OmnigentError("No review outputs have been saved yet", code=ErrorCode.NOT_FOUND)
        response.headers["Cache-Control"] = "private, no-store"
        return manifest

    @router.get("/sessions/{session_id}/design-review/outputs/files/{path:path}")
    async def get_output_file(request: Request, session_id: str, path: str) -> Response:
        root_id = await _root_id(request, session_id)
        try:
            found = await asyncio.to_thread(read_output, artifact_store, root_id, path)
        except OutputPublicationError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INTERNAL_ERROR) from exc
        if found is None:
            raise OmnigentError("No saved review output at this path", code=ErrorCode.NOT_FOUND)
        data, sha256 = found
        name = path.rsplit("/", 1)[-1].replace('"', "")
        return Response(
            content=data,
            # Never rendered as HTML: outputs are agent-written, untrusted content.
            media_type="text/plain; charset=utf-8",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-SHA256": sha256,
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
