"""Cloud Design review pinning and confinement checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.server.egm_cloud import (
    CloudReviewError,
    CloudReviewTarget,
    checkout_command,
    cloud_review_labels,
    evaluate_probe,
    pinned_commit,
    resolve_commit_sync,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clone_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone at the newer commit of an origin with two commits."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    (origin / "README.md").write_text("one\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "--quiet", "-m", "one")
    first = _git(origin, "rev-parse", "HEAD")
    (origin / "README.md").write_text("two\n")
    _git(origin, "commit", "--quiet", "-am", "two")
    second = _git(origin, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    return clone, first, second


def test_checkout_pins_commit_and_prepares_output(tmp_path: Path) -> None:
    clone, first, _ = _clone_with_two_commits(tmp_path)

    subprocess.run(["sh", "-c", checkout_command(str(clone), first)], check=True)

    assert _git(clone, "rev-parse", "HEAD") == first
    assert (clone / "README.md").read_text() == "one\n"
    assert (clone / "docs/designs/egm").is_dir()


def test_checkout_rejects_symlinked_output_path(tmp_path: Path) -> None:
    clone, _, second = _clone_with_two_commits(tmp_path)
    (clone / "docs").symlink_to(tmp_path)

    completed = subprocess.run(
        ["sh", "-c", checkout_command(str(clone), second)], capture_output=True, text=True
    )

    assert completed.returncode == 3
    assert "symlinked output path: docs" in completed.stderr
    assert not (tmp_path / "designs").exists()


@pytest.mark.parametrize("commit", ["HEAD", "a" * 39, "A" * 40, "a" * 40 + "; rm -rf /"])
def test_checkout_rejects_anything_but_a_full_sha(commit: str) -> None:
    with pytest.raises(ValueError):
        checkout_command("/root/workspace/repo", commit)


@pytest.mark.parametrize(
    ("report", "error"),
    [
        ({"inside": True, "root": False, "home": False}, None),
        ({"inside": True, "root": True, "home": False}, "could not confine"),
        ({"inside": True, "root": False, "home": True}, "could not confine"),
        ({"inside": False, "root": False, "home": False}, "could not confine"),
        (None, "could not verify"),
    ],
)
def test_probe_must_prove_writes_are_confined(report: dict | None, error: str | None) -> None:
    # Providers may merge stderr noise ahead of the report line.
    stdout = "bwrap: warning\n" + (json.dumps({**report, "exit_code": 0}) if report else "")
    if error is None:
        evaluate_probe(stdout)
        return
    with pytest.raises(CloudReviewError, match=error):
        evaluate_probe(stdout)


def test_pinned_commit_reads_only_cloud_review_labels() -> None:
    commit = "b" * 40
    labels = cloud_review_labels(
        CloudReviewTarget(repository="https://github.com/org/repo.git", branch=None, commit=commit)
    )

    assert pinned_commit(labels) == commit
    assert pinned_commit({**labels, "omnigent.egm.cloud": "false"}) is None
    assert pinned_commit({**labels, "omnigent.egm.commit": "main"}) is None
    assert pinned_commit(None) is None


@pytest.mark.parametrize("repository", ["git@github.com:org/repo.git", "file:///tmp/repo"])
def test_resolve_commit_requires_https(repository: str) -> None:
    with pytest.raises(CloudReviewError, match="https://"):
        resolve_commit_sync(repository, None)
