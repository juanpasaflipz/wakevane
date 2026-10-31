"""Durable cloud Design review outputs: export, storage and authorized reads."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.egm_cloud import CLOUD_LABEL, COMMIT_LABEL, REPOSITORY_LABEL
from omnigent.server.egm_outputs import (
    OutputPublicationError,
    collector_command,
    parse_collected,
    publish_outputs,
    read_manifest,
    read_output,
)
from omnigent.server.routes.design_review_outputs import create_design_review_outputs_router
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_LABELS = {
    CLOUD_LABEL: "true",
    REPOSITORY_LABEL: "https://github.com/org/repo.git",
    COMMIT_LABEL: "c" * 40,
}


class _LocalSandbox:
    """Runs sandbox commands on this machine, as a provider's ``run`` would remotely."""

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> Any:
        completed = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(stdout=completed.stdout, stderr=completed.stderr)


def _workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    workspace = tmp_path / "repo"
    for relative, text in files.items():
        path = workspace / "docs/designs/egm" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (workspace / "secrets.env").parent.mkdir(parents=True, exist_ok=True)
    (workspace / "secrets.env").write_text("TOKEN=never-exported\n")
    return workspace


def _publish(store: LocalArtifactStore, workspace: Path, reason: str = "checkpoint") -> dict:
    return publish_outputs(
        _LocalSandbox(),
        "sb-1",
        store=store,
        root_session_id="root-1",
        workspace=str(workspace),
        labels=_LABELS,
        reason=reason,
    )


def test_publish_exports_only_review_outputs_with_hashes(tmp_path: Path) -> None:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    workspace = _workspace(tmp_path, {"run/spec-r1.md": "# Spec\n", "run/review.md": "ok\n"})

    manifest = _publish(store, workspace)

    assert [entry["path"] for entry in manifest["files"]] == ["run/review.md", "run/spec-r1.md"]
    assert (manifest["commit"], manifest["checkpoint"]) == ("c" * 40, 1)
    data, sha256 = read_output(store, "root-1", "run/spec-r1.md")
    assert data == b"# Spec\n" and sha256 == hashlib.sha256(data).hexdigest()
    assert read_output(store, "root-1", "../secrets.env") is None

    # Unchanged outputs are idempotent; a change creates the next checkpoint.
    assert _publish(store, workspace)["checkpoint"] == 1
    (workspace / "docs/designs/egm/run/review.md").write_text("revised\n")
    assert _publish(store, workspace, "idle")["checkpoint"] == 2

    # Outputs stay readable after the sandbox's files are gone.
    subprocess.run(["rm", "-rf", str(workspace)], check=True)
    assert read_output(store, "root-1", "run/review.md")[0] == b"revised\n"


def test_symlink_in_outputs_blocks_publication_and_keeps_last_checkpoint(tmp_path: Path) -> None:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    workspace = _workspace(tmp_path, {"spec.md": "v1\n"})
    _publish(store, workspace)
    (workspace / "docs/designs/egm/leak.env").symlink_to(workspace / "secrets.env")

    with pytest.raises(OutputPublicationError, match=r"symlink: leak\.env"):
        _publish(store, workspace)

    assert [entry["path"] for entry in read_manifest(store, "root-1")["files"]] == ["spec.md"]


def test_collector_refuses_an_output_directory_that_is_a_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    (workspace / "docs/designs").mkdir(parents=True)
    (workspace / "docs/designs/egm").symlink_to(tmp_path)

    completed = subprocess.run(
        ["sh", "-c", collector_command(str(workspace))], capture_output=True, text=True
    )

    with pytest.raises(OutputPublicationError, match="missing or is a symlink"):
        parse_collected(completed.stdout)


def _report(path: str, data: bytes, **overrides: Any) -> str:
    entry = {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": base64.b64encode(data).decode(),
        **overrides,
    }
    return json.dumps({"files": [entry], "errors": []})


@pytest.mark.parametrize(
    ("report", "error"),
    [
        (_report("../escape.md", b"x"), "unsafe path"),
        (_report("/etc/passwd", b"x"), "unsafe path"),
        (_report("a/./b.md", b"x"), "unsafe path"),
        (_report("spec.md", b"x", sha256="0" * 64), "changed while it was exported"),
        ("no report", "did not report"),
    ],
)
def test_server_rejects_unsafe_or_inconsistent_reports(report: str, error: str) -> None:
    with pytest.raises(OutputPublicationError, match=error):
        parse_collected(report)


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


async def test_outputs_are_readable_only_with_session_access(db_uri: str, tmp_path: Path) -> None:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    root = conversations.create_conversation(agent_id="ab" * 16, labels=_LABELS)
    for user in ("owner", "other"):
        permissions.ensure_user(user)
    permissions.grant("owner", root.id, LEVEL_OWNER)
    publish_outputs(
        _LocalSandbox(),
        "sb-1",
        store=store,
        root_session_id=root.id,
        workspace=str(_workspace(tmp_path, {"spec.md": "# Spec\n"})),
        labels=_LABELS,
        reason="checkpoint",
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.include_router(
        create_design_review_outputs_router(
            artifact_store=store,
            conversation_store=conversations,
            permission_store=permissions,
            auth_provider=_HeaderAuth(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    base = f"/v1/sessions/{root.id}/design-review/outputs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wakevane.test"
    ) as client:
        owner = {"x-test-user": "owner"}
        manifest = (await client.get(base, headers=owner)).json()
        assert [entry["path"] for entry in manifest["files"]] == ["spec.md"]
        download = await client.get(f"{base}/files/spec.md", headers=owner)
        assert download.content == b"# Spec\n"
        assert download.headers["x-content-sha256"] == manifest["files"][0]["sha256"]
        assert download.headers["content-type"].startswith("text/plain")

        for path in ("", "/files/spec.md"):
            denied = await client.get(base + path, headers={"x-test-user": "other"})
            assert denied.status_code == 404
        assert (await client.get(base)).status_code == 401
