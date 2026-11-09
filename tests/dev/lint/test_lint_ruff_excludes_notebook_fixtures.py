"""Regression test for issue #5.

``web/src/shell/__fixtures__`` holds notebook test fixtures with
intentionally unsorted imports (``01_typical.ipynb``, ``02_edgecases.ipynb``)
and intentionally invalid JSON (``03_broken.ipynb``), used to exercise the
shell's notebook-rendering code under test. They are not real
notebooks/source for ruff to lint. Without an explicit exclude, a plain
``ruff check .`` (what Flywheel CI and the ``lint`` pre-commit hook both run)
parses them as real input and fails on every push.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "web" / "src" / "shell" / "__fixtures__"


def test_fixtures_directory_exists() -> None:
    # Guards the two checks below against silently testing nothing if the
    # fixtures are ever moved.
    assert FIXTURES_DIR.is_dir()
    assert list(FIXTURES_DIR.glob("*.ipynb"))


def test_pyproject_extend_exclude_lists_notebook_fixtures() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    excludes = config["tool"]["ruff"]["extend-exclude"]
    assert "web/src/shell/__fixtures__" in excludes


@pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="ruff is only installed via the repo's `lint` dependency group",
)
def test_ruff_check_passes_on_repo_root() -> None:
    # Exactly the repro from issue #5: a plain `ruff check .` must not trip
    # over the notebook fixtures' unsorted imports / invalid JSON.
    result = subprocess.run(
        ["ruff", "check", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
