"""The one-call ladder template (``POST /v1/templates/ladder``).

Installs a fixer agent for one repository as the caller's own agent, and
describes the ladder that runs it once per model, cheapest first. The fixer
is the software factory's fixer run on its own: same sandbox grants, same
push policy and, on managed sandboxes, the same GitHub credential proxy, so a
session can clone, push a branch and open a pull request without being asked.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omnigent.server.factory_template import (
    FACTORY_BUNDLE_SOURCE,
    _function_policy,
    apply_managed_fixer_sandbox,
)

_FIXER_SOURCE = FACTORY_BUNDLE_SOURCE / "agents" / "fixer" / "config.yaml"
_COST_BUDGET_PATH = "omnigent.policies.builtins.cost.cost_budget"

DEFAULT_TRIGGER_KIND = "issues.opened"
# Spend cap for one attempt when a rung names none.
DEFAULT_RUNG_MAX_COST_USD = 5.0
MAX_RUNG_MAX_COST_USD = 100.0

LADDER_PROMPT_TEMPLATE = (
    "Fix {task} in the GitHub repository {repo}. The event that started this task "
    "is at the end of this message."
)

_FIXER_PROMPT = """\
You fix one task in one GitHub repository and prove the fix with the
repository's own tests. Nobody is watching this session: never ask a question
and never wait for an answer.

Each task message names the repository, what the task is about (for example
`issue:42` or `pr:7`) and the exact branch you must push.

## Method
1. Clone the repository into the current directory (it persists across shell
   calls; /tmp does not): `gh repo clone <owner/name> .` Set a repo-local
   identity: `git config user.name "WakeVane ladder"` and
   `git config user.email "ladder@wakevane.com"`.
2. Read the task. For an issue: `gh issue view <number> --repo <owner/name>`.
   For a pull request or a failed check, read the pull request and the
   failing check's log.
3. If the message names an earlier attempt's branch, fetch it and start from
   it when its work is sound; otherwise start from the default branch. Create
   the branch named in the message, exactly as written.
4. Reproduce first: run the tests and watch them fail. Then fix the root
   cause with the smallest change that makes them pass. Do not weaken, skip
   or delete a test to get a pass.
5. Run the full test command again and read the result.
6. Stage only the files you changed (`git add <path>...`), commit with a
   message that names the scenario, push the branch and open a pull request
   with `gh pr create` that references the task. Never merge.

If you cannot make the tests pass, do not push. Say what you tried and why it
failed; a stronger model will take over.

## Report (your final message, nothing else)
    pr: <url, or "none">
    tests: <command> -> <summary line>
    risk: <one line on what could still be wrong>
"""


@dataclass(frozen=True)
class LadderTemplateRung:
    """
    One step of the templated ladder.

    :param model: Model the fixer runs on for this step, e.g.
        ``"claude-haiku-4-5"``.
    :param max_cost_usd: Spend cap for one attempt on this step.
    """

    model: str
    max_cost_usd: float = DEFAULT_RUNG_MAX_COST_USD


def ladder_fixer_agent_name(owner: str, repo: str) -> str:
    """The fixer's agent name for one repository, e.g. ``ladder-fixer-acme-api``.

    Agent names allow only letters, digits, ``-`` and ``_``, so dots and other
    characters in GitHub names collapse to ``-``.
    """
    return re.sub(r"[^a-z0-9_-]+", "-", f"ladder-fixer-{owner}-{repo}".lower()).strip("-")


def ladder_name(owner: str, repo: str, trigger_kind: str) -> str:
    """The templated ladder's name, e.g. ``"acme/api: issues.opened"``."""
    return f"{owner}/{repo}: {trigger_kind}"


def ladder_fixer_config(
    owner: str, repo: str, *, max_cost_usd: float, managed_sandbox: bool
) -> dict[str, Any]:
    """
    Build the standalone fixer's ``config.yaml`` for ``owner/repo``.

    Starts from the factory fixer's config so the sandbox grants and the
    push policy stay in one place, then replaces what is specific to running
    under a foreman: the name, the prompt and the budget.

    :param max_cost_usd: Hard spend cap for one session; the highest rung cap.
    :param managed_sandbox: Add the GitHub credential proxy and egress
        allowlist a managed (Linux) sandbox needs.
    """
    config: dict[str, Any] = yaml.safe_load(_FIXER_SOURCE.read_text(encoding="utf-8"))
    config["name"] = ladder_fixer_agent_name(owner, repo)
    config["description"] = (
        f"Fixes one task in {owner}/{repo} on the branch a ladder names, runs the "
        "tests and opens a pull request. Runs unattended."
    )
    config["prompt"] = _FIXER_PROMPT
    _function_policy(config, _COST_BUDGET_PATH)["arguments"] = {"max_cost_usd": max_cost_usd}
    if managed_sandbox:
        apply_managed_fixer_sandbox(config)
    return config


def build_ladder_fixer_bundle(
    owner: str, repo: str, *, max_cost_usd: float, managed_sandbox: bool
) -> bytes:
    """Tar the standalone fixer for ``owner/repo``.

    :returns: A deterministic gzipped tarball, ready for agent install.
    """
    # Shares the deterministic tar used for edited agent bundles.
    from omnigent.server.routes.session_mcp_servers import _tar_gz_dir

    config = ladder_fixer_config(
        owner, repo, max_cost_usd=max_cost_usd, managed_sandbox=managed_sandbox
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = Path(tmpdir) / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return _tar_gz_dir(bundle_dir)
