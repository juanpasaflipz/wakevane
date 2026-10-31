"""Approved coding stage: approval, coding outputs, funding gate and pull requests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.egm_cloud import CLOUD_LABEL, COMMIT_LABEL, REPOSITORY_LABEL
from omnigent.server.egm_coding import (
    APPROVAL_LABEL,
    CODING_BRANCH_LABEL,
    CODING_COMMIT_LABEL,
    CodingBinding,
    checkout_command,
    coding_binding,
    publish_coding_outputs,
    read_coding_blob,
    read_coding_manifest,
)
from omnigent.server.egm_outputs import OutputFile, OutputPublicationError, store_outputs
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.github_app_client import GitHubAppClient
from omnigent.server.managed_hosts import managed_repo_labels
from omnigent.server.routes.design_review_coding import create_design_review_coding_router
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_SPEC = b"# Spec r2\n\nPrint incomplete tasks.\n"
_SPEC_SHA = hashlib.sha256(_SPEC).hexdigest()


class _LocalSandbox:
    """Runs sandbox commands on this machine, as a provider's ``run`` would remotely."""

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> Any:
        completed = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        if check and completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return SimpleNamespace(stdout=completed.stdout, stderr=completed.stderr)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "keep.txt").write_text("keep\n")
    (repo / "old.txt").write_text("old\n")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _binding(commit: str) -> CodingBinding:
    return CodingBinding(
        approval_id="0123456789abcdef",
        review_session_id="review-1",
        repository="https://github.com/org/repo.git",
        commit=commit,
        branch="wakevane/design-0123456789",
    )


def test_coding_outputs_capture_changes_and_tests_durably(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    binding = _binding(base)
    sandbox = _LocalSandbox()
    sandbox.run("sb", checkout_command(str(repo), base, binding.branch))
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == binding.branch

    (repo / "keep.txt").write_text("changed\n")
    (repo / "new.sh").write_text("#!/bin/sh\necho hi\n")
    (repo / "new.sh").chmod(0o755)
    (repo / "old.txt").unlink()
    (repo / "build").mkdir()
    (repo / "build" / "ignored.bin").write_bytes(b"\0" * 10)
    (repo / ".git/wakevane/test-output.txt").write_text("$ pytest\n1 passed\nexit code: 0\n")

    def publish(reason: str) -> dict[str, Any]:
        return publish_coding_outputs(
            sandbox,
            "sb",
            store=store,
            binding=binding,
            session_id="coding-1",
            workspace=str(repo),
            reason=reason,
        )

    manifest = publish("checkpoint")
    assert [(f["path"], f["mode"], f["status"]) for f in manifest["files"]] == [
        ("keep.txt", "100644", "M"),
        ("new.sh", "100755", "A"),
    ]
    assert manifest["deleted"] == ["old.txt"]
    assert (manifest["base_commit"], manifest["checkpoint"]) == (base, 1)
    patch = read_coding_blob(store, "review-1", binding.approval_id, manifest["patch"]["sha256"])
    assert b"+changed" in patch and b"ignored.bin" not in patch
    tests = read_coding_blob(store, "review-1", binding.approval_id, manifest["tests"]["sha256"])
    assert tests.endswith(b"exit code: 0\n")

    # Unchanged work keeps the checkpoint; the evidence outlives the sandbox.
    assert publish("checkpoint")["checkpoint"] == 1
    subprocess.run(["rm", "-rf", str(repo)], check=True)
    stored = read_coding_manifest(store, "review-1", binding.approval_id)
    assert stored is not None and stored["digest"] == manifest["digest"]


def test_symlinked_change_blocks_export_and_keeps_last_checkpoint(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    binding = _binding(base)
    sandbox = _LocalSandbox()
    sandbox.run("sb", checkout_command(str(repo), base, binding.branch))
    (repo / "link").symlink_to("/etc/passwd")

    with pytest.raises(OutputPublicationError, match="unsupported file type: link"):
        publish_coding_outputs(
            sandbox,
            "sb",
            store=store,
            binding=binding,
            session_id="coding-1",
            workspace=str(repo),
            reason="checkpoint",
        )
    assert read_coding_manifest(store, "review-1", binding.approval_id) is None


def test_binding_rejects_tampered_labels() -> None:
    labels = {
        APPROVAL_LABEL: "0123456789abcdef",
        "omnigent.egm.coding.review": "review-1",
        "omnigent.egm.coding.repository": "https://github.com/org/repo.git",
        CODING_COMMIT_LABEL: "a" * 40,
        CODING_BRANCH_LABEL: "wakevane/design-0123456789",
    }
    assert coding_binding(labels) is not None
    assert coding_binding({**labels, CODING_BRANCH_LABEL: "main"}) is None
    assert coding_binding({**labels, CODING_COMMIT_LABEL: "HEAD"}) is None


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


class _Agents:
    def get_by_name(self, name: str) -> Any:
        return SimpleNamespace(id="agent-claude", managed_by=None)

    def get(self, agent_id: str) -> Any:
        return None


class _GithubConnections:
    def __init__(self) -> None:
        self.connected = True

    def get(self, user_id: str, *, with_tokens: bool = False) -> Any:
        if not self.connected:
            return None
        return SimpleNamespace(
            access_token="gho_synthetic",
            refresh_token=None,
            token_expires_at=None,
            github_login="octo",
            github_user_id=7,
        )


class _GitHub:
    """Fake GitHub REST API recording every request the server makes."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/repos/org/repo")
        routes: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", f"/git/commits/{'c' * 40}"): (200, {"tree": {"sha": "tree-base"}}),
            ("POST", "/git/blobs"): (201, {"sha": "blob-1"}),
            ("POST", "/git/trees"): (201, {"sha": "tree-new"}),
            ("POST", "/git/commits"): (201, {"sha": "d" * 40}),
            ("POST", "/git/refs"): (201, {}),
            ("GET", "/pulls"): (200, []),
            ("POST", "/pulls"): (
                201,
                {"number": 5, "html_url": "https://github.com/org/repo/pull/5"},
            ),
        }
        status, body = routes.get((request.method, path), (404, {}))
        return httpx.Response(status, json=body)


@pytest_asyncio.fixture
async def review(db_uri: str, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    labels = {
        CLOUD_LABEL: "true",
        REPOSITORY_LABEL: "https://github.com/org/repo.git",
        COMMIT_LABEL: "c" * 40,
        **managed_repo_labels(["https://github.com/org/repo.git#main"]),
    }
    root = conversations.create_conversation(agent_id="ab" * 16, labels=labels)
    plain = conversations.create_conversation(agent_id="ab" * 16)
    for user in ("owner", "reader", "other"):
        permissions.ensure_user(user)
    for session in (root, plain):
        permissions.grant("owner", session.id, LEVEL_OWNER)
    permissions.grant("reader", root.id, LEVEL_READ)
    store_outputs(
        store,
        root_session_id=root.id,
        labels=labels,
        files=[OutputFile(path="run/r2.md", sha256=_SPEC_SHA, data=_SPEC)],
        reason="checkpoint",
    )
    created: list[dict[str, Any]] = []

    async def create_json_session(
        request: Request, body: Any, user_id: str | None, *, server_labels: dict[str, str]
    ) -> Any:
        created.append({"body": body, "user": user_id, "labels": server_labels})
        return conversations.create_conversation(agent_id="cd" * 16, labels=server_labels)

    github = _GitHub()
    connections = _GithubConnections()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.state.feature_flags = FeatureFlags(frozenset({Feature.EGM_CLOUD_CODING}))
    app.state.create_json_session = create_json_session
    app.state.github_store = connections
    app.state.github_client = GitHubAppClient(
        SimpleNamespace(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(github.handler),
    )
    app.include_router(
        create_design_review_coding_router(
            artifact_store=store,
            conversation_store=conversations,
            agent_store=_Agents(),  # type: ignore[arg-type]
            permission_store=permissions,
            auth_provider=_HeaderAuth(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wakevane.test"
    ) as client:
        yield {
            "client": client,
            "app": app,
            "root": root,
            "plain": plain,
            "store": store,
            "conversations": conversations,
            "created": created,
            "github": github,
            "connections": connections,
        }


def _approve(review: dict[str, Any], user: str = "owner", **body: Any) -> Any:
    payload = {"path": "run/r2.md", "sha256": _SPEC_SHA, **body}
    return review["client"].post(
        f"/v1/sessions/{review['root'].id}/design-review/approve",
        json=payload,
        headers={"x-test-user": user},
    )


async def test_owner_approval_starts_one_bound_coding_session(review: dict[str, Any]) -> None:
    response = await _approve(review, commit="c" * 40)
    assert response.status_code == 200, response.text
    approval = response.json()["approval"]
    assert response.json()["created"] is True
    assert (approval["approved_by"], approval["spec_sha256"], approval["commit"]) == (
        "owner",
        _SPEC_SHA,
        "c" * 40,
    )
    coding = review["conversations"].get_conversation(approval["coding_session_id"])
    assert coding is not None and coding.root_conversation_id in (None, coding.id)
    assert approval["base_branch"] == "main"

    [created] = review["created"]
    body = created["body"]
    assert (body.host_type, body.workspace, body.agent_id) == (
        "managed",
        "https://github.com/org/repo.git#main",
        "agent-claude",
    )
    assert body.parent_session_id is None
    text = body.initial_input.data["content"][0]["text"]
    assert _SPEC.decode() in text and _SPEC_SHA in text
    binding = coding_binding(created["labels"])
    assert binding is not None
    assert (binding.commit, binding.branch) == ("c" * 40, approval["coding_branch"])

    # Repeating the approval is idempotent: no second coding session.
    again = await _approve(review)
    assert again.status_code == 200 and again.json()["created"] is False
    assert len(review["created"]) == 1
    listed = await review["client"].get(
        f"/v1/sessions/{review['root'].id}/design-review/approvals",
        headers={"x-test-user": "reader"},
    )
    assert [item["id"] for item in listed.json()["approvals"]] == [approval["id"]]


@pytest.mark.parametrize(
    ("user", "body", "status"),
    [
        pytest.param("owner", {"sha256": "0" * 64}, 409, id="hash-mismatch"),
        pytest.param("owner", {"commit": "e" * 40}, 409, id="other-commit"),
        pytest.param("owner", {"path": "run/missing.md"}, 404, id="unknown-path"),
        pytest.param("reader", {}, 403, id="reader-not-owner"),
        pytest.param("other", {}, 404, id="no-access"),
    ],
)
async def test_rejected_approvals_record_and_start_nothing(
    review: dict[str, Any], user: str, body: dict[str, Any], status: int
) -> None:
    response = await _approve(review, user=user, **body)

    assert response.status_code == status, response.text
    assert review["created"] == []
    assert not review["store"].exists(f"egm-approvals/{review['root'].id}/index.json")


async def test_approval_requires_flag_and_a_cloud_review(review: dict[str, Any]) -> None:
    plain = await review["client"].post(
        f"/v1/sessions/{review['plain'].id}/design-review/approve",
        json={"path": "run/r2.md", "sha256": _SPEC_SHA},
        headers={"x-test-user": "owner"},
    )
    assert plain.status_code == 400
    review["app"].state.feature_flags = FeatureFlags()
    off = await _approve(review)
    assert off.status_code == 404
    assert "not enabled" in off.json()["message"]
    assert review["created"] == []


def _store_coding_changes(review: dict[str, Any], approval: dict[str, Any]) -> None:
    from omnigent.server.egm_coding import store_coding_outputs

    binding = coding_binding(
        {
            APPROVAL_LABEL: approval["id"],
            "omnigent.egm.coding.review": review["root"].id,
            "omnigent.egm.coding.repository": approval["repository"],
            CODING_COMMIT_LABEL: approval["commit"],
            CODING_BRANCH_LABEL: approval["coding_branch"],
        }
    )
    assert binding is not None
    store_coding_outputs(
        review["store"],
        binding=binding,
        session_id="coding-1",
        report={
            "files": [
                {"path": "tasks.py", "mode": "100644", "status": "A", "data": b"print(1)\n"}
            ],
            "deleted": ["old.txt"],
            "patch": b"diff --git a/tasks.py b/tasks.py\n",
            "patch_truncated": False,
            "tests": b"exit code: 0\n",
            "tests_truncated": False,
            "head": None,
        },
        reason="checkpoint",
    )


async def test_pull_request_opens_from_stored_changes_and_never_merges(
    review: dict[str, Any],
) -> None:
    approval = (await _approve(review)).json()["approval"]
    url = f"/v1/sessions/{review['root'].id}/design-review/approvals/{approval['id']}/pull-request"
    owner = {"x-test-user": "owner"}

    no_changes = await review["client"].post(url, headers=owner)
    assert no_changes.status_code == 409
    _store_coding_changes(review, approval)
    assert (await review["client"].post(url, headers={"x-test-user": "reader"})).status_code == 403

    review["connections"].connected = False
    unconnected = await review["client"].post(url, headers=owner)
    assert unconnected.status_code == 400
    assert "Connect your GitHub account" in unconnected.json()["message"]
    review["connections"].connected = True

    opened = await review["client"].post(url, headers=owner)
    assert opened.status_code == 200, opened.text
    pull = opened.json()["approval"]["pull_request"]
    assert (pull["number"], pull["branch"], pull["base"]) == (5, approval["coding_branch"], "main")

    requests = review["github"].requests
    assert all(request.method in {"GET", "POST"} for request in requests)
    assert not any("merge" in request.url.path for request in requests)
    tree = json.loads(next(r for r in requests if r.url.path.endswith("/git/trees")).content)
    assert tree["base_tree"] == "tree-base"
    assert {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    commit = json.loads(next(r for r in requests if r.url.path.endswith("/git/commits")).content)
    assert commit["parents"] == ["c" * 40]
    assert commit["author"]["email"] == "7+octo@users.noreply.github.com"
    created = json.loads(
        next(r for r in requests if r.url.path.endswith("/pulls") and r.method == "POST").content
    )
    assert created["head"] == approval["coding_branch"] and _SPEC_SHA in created["body"]

    # Opening again returns the recorded pull request without touching GitHub.
    before = len(requests)
    again = await review["client"].post(url, headers=owner)
    assert again.json()["created"] is False and len(requests) == before


async def test_trial_coding_launch_without_coding_grant_creates_no_sandbox(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from omnigent.server.funded_trial import FundedTrialConfig, FundedTrialPolicy
    from omnigent.server.funded_trial_store import FundedTrialStore
    from omnigent.server.managed_hosts import ManagedLaunchTracker, parse_sandbox_config
    from omnigent.server.routes._sessions import orchestration

    store = FundedTrialStore(db_uri)
    policy = FundedTrialPolicy(
        enabled=True,
        store=store,
        config=FundedTrialConfig.from_mapping(
            {
                "models": {
                    "claude-test": {
                        "input": 3,
                        "output": 15,
                        "cache_read": 0.3,
                        "cache_write": 3.75,
                    }
                },
                "compute_usd_per_hour": {"modal": 0.5},
            }
        ),
        environ={"OMNIGENT_FUNDED_TRIAL_SIGNING_KEY": "s" * 40},
    )
    deployment = parse_sandbox_config(
        {"provider": "modal", "server_url": "https://s.example.com", "funding": "trial"}
    )
    assert deployment is not None
    deployment.funded_trial = policy
    labels = {
        APPROVAL_LABEL: "0123456789abcdef",
        "omnigent.egm.coding.review": "review-1",
        "omnigent.egm.coding.repository": "https://github.com/org/repo.git",
        CODING_COMMIT_LABEL: "a" * 40,
        CODING_BRANCH_LABEL: "wakevane/design-0123456789",
    }
    conversation = SimpleNamespace(
        id="coding-1", labels=labels, root_conversation_id=None, inference_snapshot=None
    )
    provisioned: list[dict[str, Any]] = []

    async def _provision(**kwargs: Any) -> None:
        held = [charge.root_session_id for charge in store.open_charges("owner")]
        provisioned.append({**kwargs, "held_roots": held})

    monkeypatch.setattr(orchestration, "_provision_managed_sandbox", _provision)

    async def launch(owner: str) -> ManagedLaunchTracker:
        tracker = ManagedLaunchTracker()
        tracker.begin("coding-1")
        await orchestration._run_managed_launch(
            session_id="coding-1",
            owner=owner,
            sandbox_config=deployment,
            repos=[],
            tracker=tracker,
            conversation_store=SimpleNamespace(get_conversation=lambda _sid: conversation),
            host_store=SimpleNamespace(),
            host_registry=None,
            tunnel_registry=None,
        )
        return tracker

    store.upsert_grant(
        "owner",
        allowance_micros=100_000_000,
        expires_at=int(time.time()) + 3600,
        capabilities={"inference", "compute"},
        max_concurrent_roots=1,
        actor_id="operator",
    )
    tracker = await launch("owner")
    assert provisioned == []
    entry = tracker.get("coding-1")
    assert entry is not None and "denied" in (entry.error or "")
    assert store.open_charges("owner") == []

    store.upsert_grant(
        "owner",
        allowance_micros=100_000_000,
        expires_at=int(time.time()) + 3600,
        capabilities={"inference", "compute", "coding"},
        max_concurrent_roots=1,
        actor_id="operator",
    )
    await launch("owner")
    [kwargs] = provisioned
    assert kwargs["prepare_workspace"] is not None
    # The coding launch holds compute under the review's funded workflow.
    assert kwargs["held_roots"] == ["review-1"]
