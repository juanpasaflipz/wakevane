"""Plain data types for ladders and their attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Branch an attempt must push, so a check result can be tied back to it.
ATTEMPT_BRANCH_PREFIX = "wakevane/att-"

# Check events an attempt can be judged by.
VERIFY_KINDS: frozenset[str] = frozenset({"check_suite.completed", "check_run.completed"})
DEFAULT_VERIFY_KIND = "check_suite.completed"
DEFAULT_ATTEMPT_TIMEOUT_MINUTES = 120
MAX_RUNGS = 5


class LadderState(StrEnum):
    """Lifecycle state of a :class:`Ladder`."""

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class AttemptStatus(StrEnum):
    """How a :class:`LadderAttempt` stands."""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"


# Endings that hand the task to the next rung.
ESCALATING_STATUSES: frozenset[str] = frozenset(
    {AttemptStatus.FAILED.value, AttemptStatus.TIMED_OUT.value}
)


@dataclass(frozen=True)
class Rung:
    """
    One step of a ladder: the agent (and optional model) that tries the task.

    :param agent_id: Agent a session is started with.
    :param model_override: Model for that session, e.g. ``"claude-haiku-4-5"``;
        ``None`` uses the agent's own.
    :param max_cost_usd: Spend cap for one attempt on this rung.
    :param label: Name shown on the scoreboard, e.g. ``"cheap"``.
    :param permission_mode: Native-harness permission mode for the session,
        e.g. ``"bypassPermissions"``; ``None`` keeps the harness default,
        which asks before acting.
    """

    agent_id: str
    model_override: str | None = None
    max_cost_usd: float | None = None
    label: str | None = None
    permission_mode: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The rung as stored and returned by the API."""
        return {
            "agent_id": self.agent_id,
            "model_override": self.model_override,
            "max_cost_usd": self.max_cost_usd,
            "label": self.label,
            "permission_mode": self.permission_mode,
        }


@dataclass(frozen=True)
class Ladder:
    """
    A ladder as read from the store.

    :param repo: Repository watched, e.g. ``"acme/api"``.
    :param trigger_kind: Event kind that starts a task, e.g. ``"issues.opened"``.
    :param trigger_match: Extra field filter applied to the trigger event.
    :param rungs: Agents to try in order, cheapest first.
    :param verify_kind: Check event that judges an attempt.
    :param verify_name: Check name the verdict is read from; ``None`` for any.
    """

    id: str
    user_id: str | None
    name: str
    repo: str
    trigger_kind: str
    prompt_template: str
    rungs: tuple[Rung, ...]
    created_at: int
    workspace_id: int = 0
    account_generation: str | None = None
    trigger_match: dict[str, Any] = field(default_factory=dict)
    verify_kind: str = DEFAULT_VERIFY_KIND
    verify_name: str | None = None
    execution_target: str = "managed_sandbox"
    host_id: str | None = None
    workspace: str | None = None
    attempt_timeout_minutes: int = DEFAULT_ATTEMPT_TIMEOUT_MINUTES
    state: str = LadderState.ACTIVE.value
    updated_at: int | None = None


@dataclass(frozen=True)
class LadderAttempt:
    """
    One session a ladder started, as read from the store.

    :param task_id: Shared by every rung tried on the same triggering event.
    :param task_key: What the task concerns, e.g. ``"issue:42"``.
    :param event_fields: The triggering event's fields.
    """

    id: str
    ladder_id: str
    task_id: str
    user_id: str | None
    repo: str
    task_key: str
    rung_index: int
    agent_id: str
    status: str
    started_at: int
    workspace_id: int = 0
    model_override: str | None = None
    model: str | None = None
    conversation_id: str | None = None
    pull_request: int | None = None
    detail: str | None = None
    event_fields: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    finished_at: int | None = None

    @property
    def branch(self) -> str:
        """The branch this attempt was told to push."""
        return attempt_branch(self.id)


def attempt_branch(attempt_id: str) -> str:
    """Branch name for an attempt, e.g. ``"wakevane/att-0f3c…"``."""
    return f"{ATTEMPT_BRANCH_PREFIX}{attempt_id}"


_BRANCH_REF_PREFIX = "refs/heads/"


def attempt_id_from_branch(branch: object) -> str | None:
    """
    Read the attempt id out of a branch name or a branch ref.

    :param branch: A head branch from an event, e.g. ``"wakevane/att-0f3c…"``,
        or a push ref, e.g. ``"refs/heads/wakevane/att-0f3c…"``.
    :returns: The 32-char hex attempt id, or ``None`` for any other branch.
    """
    if isinstance(branch, str) and branch.startswith(_BRANCH_REF_PREFIX):
        branch = branch[len(_BRANCH_REF_PREFIX) :]
    if not isinstance(branch, str) or not branch.startswith(ATTEMPT_BRANCH_PREFIX):
        return None
    candidate = branch[len(ATTEMPT_BRANCH_PREFIX) :]
    if len(candidate) != 32 or any(char not in "0123456789abcdef" for char in candidate):
        return None
    return candidate
