"""Browser-only Design review: pinned-commit cloud workspaces with verified confinement.

A cloud review runs in a managed sandbox instead of the user's connected host.
The server resolves the requested branch to an exact commit at creation and
records it on the session. After the sandbox is provisioned, and before any
agent starts, :func:`prepare_cloud_review` checks out that commit, prepares
the review output directory and proves the sandbox's own backend confines
writes to it. Any failure blocks the launch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

OUTPUT_DIR = "docs/designs/egm"
CLOUD_LABEL = "omnigent.egm.cloud"
COMMIT_LABEL = "omnigent.egm.commit"
REPOSITORY_LABEL = "omnigent.egm.repository"
_SHA = re.compile(r"[0-9a-f]{40}")
_LS_REMOTE_TIMEOUT_S = 30.0

# Runs inside the sandbox with the image's own omnigent package, so it proves the
# exact bwrap policy the reviewers get: writes allowed only under OUTPUT_DIR.
_PROBE_SOURCE = r"""
import json, os, subprocess, sys
from pathlib import Path
from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

workspace = Path(sys.argv[1]).resolve()
output = sys.argv[2]
backend = BwrapSandboxBackend()
policy = backend.resolve(
    OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", read_paths=["."], write_paths=[output]),
    ),
    workspace,
)
check = (
    "import json, os, sys\n"
    "def attempt(path):\n"
    "    try:\n"
    "        with open(path, 'w') as handle:\n"
    "            handle.write('probe')\n"
    "        os.remove(path)\n"
    "        return True\n"
    "    except OSError:\n"
    "        return False\n"
    "print(json.dumps({'inside': attempt(sys.argv[1]), 'root': attempt(sys.argv[2]),"
    " 'home': attempt(sys.argv[3])}))\n"
)
argv = backend.wrap_launcher_argv(
    [sys.executable, "-c", check,
     str(workspace / output / ".wakevane-probe"),
     str(workspace / ".wakevane-probe"),
     os.path.expanduser("~/.wakevane-probe")],
    policy,
    workspace,
)
completed = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
lines = completed.stdout.strip().splitlines()
result = json.loads(lines[-1]) if completed.returncode == 0 and lines else {}
result["exit_code"] = completed.returncode
result["stderr"] = completed.stderr[-300:]
print(json.dumps(result))
"""


class CloudReviewError(Exception):
    """A cloud review cannot start safely; the message is shown to the user."""


@dataclass(frozen=True)
class CloudReviewTarget:
    """The repository and exact commit a cloud review is pinned to."""

    repository: str
    branch: str | None
    commit: str


def _git_env() -> dict[str, str]:
    # Never prompt, never read the server operator's git credentials or config.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "HOME": "/nonexistent",
    }


def resolve_commit_sync(repository: str, branch: str | None) -> str:
    """Resolve *branch* (or the default branch) of a public HTTPS repository to a SHA."""
    if not repository.startswith("https://"):
        raise CloudReviewError("Cloud Design review requires an https:// repository URL.")
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--", repository, ref],
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=_LS_REMOTE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CloudReviewError("Could not reach the repository to resolve its revision.") from exc
    if completed.returncode != 0:
        raise CloudReviewError(
            "Could not read the repository. Private repositories need the WakeVane GitHub App."
        )
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref and _SHA.fullmatch(sha):
            return sha
    raise CloudReviewError(f"Branch {branch!r} was not found in the repository.")


async def resolve_cloud_target(workspace: str | None) -> CloudReviewTarget:
    """Validate a managed create's repository workspace and pin its commit."""
    from omnigent.server.managed_hosts import parse_repo_workspace

    if not workspace:
        raise OmnigentError(
            "Cloud Design review needs a repository URL", code=ErrorCode.INVALID_INPUT
        )
    try:
        repo = parse_repo_workspace(workspace)
    except ValueError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    try:
        commit = await asyncio.to_thread(resolve_commit_sync, repo.url, repo.branch)
    except CloudReviewError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    return CloudReviewTarget(repository=repo.url, branch=repo.branch, commit=commit)


def cloud_review_labels(target: CloudReviewTarget) -> dict[str, str]:
    """Session labels recording the pinned source of a cloud review."""
    return {CLOUD_LABEL: "true", REPOSITORY_LABEL: target.repository, COMMIT_LABEL: target.commit}


def pinned_commit(labels: dict[str, str] | None) -> str | None:
    """The commit a session's cloud review is pinned to, if it is one."""
    if not labels or labels.get(CLOUD_LABEL) != "true":
        return None
    commit = labels.get(COMMIT_LABEL)
    return commit if commit and _SHA.fullmatch(commit) else None


def checkout_command(workspace: str, commit: str) -> str:
    """Shell command pinning the clone to *commit* and preparing the output directory."""
    if not _SHA.fullmatch(commit):
        raise ValueError("commit must be a full lowercase SHA-1")
    ws = shlex.quote(workspace)
    return (
        "set -eu; "
        f"cd {ws}; "
        f"git cat-file -e {commit}^{{commit}} 2>/dev/null "
        f"|| git fetch --quiet --depth=1 origin {commit}; "
        f"git -c advice.detachedHead=false checkout --quiet --detach {commit}; "
        f'test "$(git rev-parse HEAD)" = {commit}; '
        # A symlink anywhere on the output path could redirect writes outside the clone.
        "for part in docs docs/designs docs/designs/egm; do "
        'if [ -L "$part" ]; then echo "symlinked output path: $part" >&2; exit 3; fi; done; '
        f"mkdir -p {OUTPUT_DIR}; "
        f'case "$(realpath {OUTPUT_DIR})" in "$(realpath .)/{OUTPUT_DIR}") ;; '
        '*) echo "output directory escapes the repository" >&2; exit 3;; esac'
    )


def probe_command(workspace: str) -> str:
    """Shell command running the confinement probe with the sandbox's own backend."""
    return shlex.join(["python3", "-c", _PROBE_SOURCE, workspace, OUTPUT_DIR])


def evaluate_probe(stdout: str) -> None:
    """Raise unless the probe proved writes are confined to the output directory."""
    result: dict[str, Any] = {}
    # Providers may merge stderr into stdout; the probe's report is its last JSON line.
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "exit_code" in parsed:
            result = parsed
            break
    if not result:
        raise CloudReviewError("The cloud workspace could not verify its sandbox.")
    if (
        result.get("inside") is True
        and result.get("root") is False
        and result.get("home") is False
    ):
        return
    raise CloudReviewError(
        "The cloud workspace could not confine review writes, so the review was not started."
    )


def prepare_cloud_review(launcher: Any, sandbox_id: str, workspace: str, commit: str) -> None:
    """Pin the commit, prepare outputs and verify confinement inside the sandbox.

    Blocking; call from a worker thread. Raises :class:`CloudReviewError`.
    """
    import click

    try:
        launcher.run(sandbox_id, checkout_command(workspace, commit))
    except click.ClickException as exc:
        raise CloudReviewError(
            "Could not check out the selected revision in the cloud workspace."
        ) from exc
    try:
        result = launcher.run(sandbox_id, probe_command(workspace), check=False)
    except click.ClickException as exc:
        raise CloudReviewError("The cloud workspace could not verify its sandbox.") from exc
    evaluate_probe(result.stdout)
