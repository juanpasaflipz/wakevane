"""Run ladders off the event bus: start tasks, judge attempts, escalate.

A trigger event starts a task on a ladder's first rung. The session is told to
push a branch named after the attempt, so the repository's own check result
for that branch is the verdict: success ends the task, failure (or silence
past the timeout) hands it to the next rung.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from omnigent.server.events.bus import CanonicalEvent, event_matches, render_template
from omnigent.server.events.delivery import LAUNCHED_BY_LABEL
from omnigent.server.ladders.model import (
    DEFAULT_ATTEMPT_TIMEOUT_MINUTES,
    ESCALATING_STATUSES,
    VERIFY_KINDS,
    AttemptStatus,
    Ladder,
    LadderAttempt,
    LadderState,
    attempt_id_from_branch,
)
from omnigent.server.ladders.store import LadderStore
from omnigent.server.scheduled.fire import LaunchOutcome, LaunchRequest

_logger = logging.getLogger(__name__)

LADDER_LABEL = "omnigent.ladder"
LADDER_ATTEMPT_LABEL = "omnigent.ladder_attempt"

_PASSING_CONCLUSIONS = frozenset({"success"})
_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out"})
_TITLE_MAX_CHARS = 120
_EVENT_JSON_MAX_CHARS = 4_000


@dataclass(frozen=True)
class SessionSpend:
    """
    What an attempt's session has cost so far.

    :param cost_usd: Recorded spend, or ``None`` when the session is unpriced.
    :param model: Model that spent the most, e.g. ``"claude-sonnet-4-5"``.
    """

    cost_usd: float | None
    model: str | None


@dataclass(frozen=True)
class SessionState:
    """
    Where an attempt's session stands, as last reported by its runner.

    :param status: ``"running"``, ``"idle"``, ``"waiting"`` or ``"failed"``;
        ``None`` when the runner never reported.
    :param updated_at: Epoch seconds of the session's last activity.
    :param pending_approvals: Approval prompts nobody has answered.
    :param error: The session's last task error, if any.
    :param model_tokens: Tokens the session's models have read and written
        so far; ``None`` when usage could not be read.
    """

    status: str | None
    updated_at: int
    pending_approvals: int = 0
    error: str | None = None
    model_tokens: int | None = None


#: Starts one session and reports how the launch ended.
Launch = Callable[[LaunchRequest], Awaitable[LaunchOutcome]]
#: Reads an attempt's session spend; ``None`` when it cannot be read.
SpendReader = Callable[[LadderAttempt], SessionSpend | None]
#: Reads an attempt's session state; ``None`` when the session is gone.
SessionProbe = Callable[[LadderAttempt], SessionState | None]

# How long a session must sit unchanged before its state is acted on, so a
# turn boundary or a slow push is not mistaken for the end of the work.
SESSION_SETTLE_SECONDS = 300
PROGRESS_BRANCH_PUSHED = "branch pushed"


def task_key_for(event: CanonicalEvent) -> str:
    """
    Name what an event is about inside its repository.

    :returns: e.g. ``"pr:412"``, ``"issue:42"`` or ``"branch:main"``.
    """
    fields = event.fields
    for prefix, name in (("pr", "pull_request"), ("issue", "issue"), ("branch", "head_branch")):
        value = fields.get(name)
        if isinstance(value, (int, str)) and not isinstance(value, bool) and value != "":
            return f"{prefix}:{value}"
    ref = fields.get("ref")
    if isinstance(ref, str) and ref:
        return f"ref:{ref}"
    return f"event:{event.received_at}"


def build_attempt_prompt(
    ladder: Ladder,
    attempt: LadderAttempt,
    *,
    kind: str,
    previous: LadderAttempt | None,
) -> str:
    """
    Build the first user message of an attempt's session.

    The ladder's own template comes first; the fixed brief after it names the
    branch the verdict is read from and, on a later rung, the attempt before.

    :param kind: The triggering event kind, e.g. ``"issues.opened"``.
    :param previous: The attempt this one takes over from, if any.
    """
    fields_json = json.dumps(attempt.event_fields, separators=(",", ":"), sort_keys=True)
    if len(fields_json) > _EVENT_JSON_MAX_CHARS:
        fields_json = fields_json[:_EVENT_JSON_MAX_CHARS] + "…[truncated]"
    rendered = render_template(
        ladder.prompt_template,
        {
            "repo": ladder.repo,
            "kind": kind,
            "task": attempt.task_key,
            "branch": attempt.branch,
            "fields_json": fields_json,
        },
    ).strip()
    brief = [
        f"[System: ladder attempt {attempt.id}, rung {attempt.rung_index + 1} of "
        f"{len(ladder.rungs)} on {ladder.repo}]",
        f"Push your work to a new branch named exactly `{attempt.branch}` and open a pull "
        "request from it. The repository's checks on that branch decide whether this attempt "
        "passed. Do not merge the pull request.",
    ]
    if previous is not None:
        opened = f" (pull request #{previous.pull_request})" if previous.pull_request else ""
        reason = previous.detail or previous.status
        brief.append(
            f"An earlier attempt did not pass: {reason}. Its work is on branch "
            f"`{previous.branch}`{opened}; you may start from it."
        )
    brief.append(fields_json)
    return "\n".join(([rendered, ""] if rendered else []) + brief)


class LadderEngine:
    """
    Reacts to bus events on behalf of every active ladder.

    :param store: Ladder and attempt persistence.
    :param launch: Starts a session for an attempt.
    :param read_spend: Reads an attempt's session cost when it ends.
    :param probe_session: Reads an attempt's session state, so an attempt
        whose session cannot finish is ended before its timeout.
    :param clock: Epoch-seconds source, injectable for tests.
    """

    def __init__(
        self,
        store: LadderStore,
        launch: Launch,
        *,
        read_spend: SpendReader | None = None,
        probe_session: SessionProbe | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._launch = launch
        self._read_spend = read_spend
        self._probe_session = probe_session
        self._clock = clock
        self._launches: set[asyncio.Task[None]] = set()

    async def handle_event(self, event: CanonicalEvent) -> None:
        """Bus listener: time out stale attempts, then judge or start work."""
        if event.source != "github":
            return
        await self.sweep()
        attempt_id = attempt_id_from_branch(
            event.fields.get("ref") if event.kind == "push" else event.fields.get("head_branch")
        )
        if attempt_id is not None:
            # An attempt's own branch is judged; it never starts another task.
            await self._on_attempt_branch_event(event, attempt_id)
            return
        await self._start_tasks(event)

    async def sweep(self) -> int:
        """
        End attempts that can no longer get a verdict, and escalate them
        when that says something about the rung.

        An attempt ends here when it outlived its ladder's timeout, or when
        its session has settled in a state no check result will follow.

        :returns: How many attempts were ended.
        """
        now = int(self._clock())
        ladders: dict[str, Ladder | None] = {}
        ended = 0
        for attempt in self._store.list_running():
            if attempt.ladder_id not in ladders:
                ladders[attempt.ladder_id] = self._store.get_ladder_any_workspace(
                    attempt.ladder_id
                )
            ladder = ladders[attempt.ladder_id]
            minutes = ladder.attempt_timeout_minutes if ladder else DEFAULT_ATTEMPT_TIMEOUT_MINUTES
            if now - attempt.started_at >= minutes * 60:
                verdict: tuple[str, str] | None = (
                    AttemptStatus.TIMED_OUT.value,
                    f"no check verdict within {minutes} minutes",
                )
            else:
                verdict = await self._session_verdict(attempt, now)
            if verdict is not None and await self._finish(attempt, ladder, *verdict):
                ended += 1
        return ended

    async def _session_verdict(self, attempt: LadderAttempt, now: int) -> tuple[str, str] | None:
        """
        Decide whether an attempt's session already rules out a check result.

        :returns: ``(status, detail)`` to end the attempt with, or ``None``
            to keep waiting.
        """
        if self._probe_session is None or attempt.conversation_id is None:
            return None
        try:
            state = await asyncio.to_thread(self._probe_session, attempt)
        except Exception:
            _logger.exception("Could not read the session of ladder attempt %s", attempt.id)
            return None
        # A session that cannot run says nothing about the rung: no escalation.
        if state is None:
            return AttemptStatus.LAUNCH_FAILED.value, "the session no longer exists"
        if state.status == "failed":
            reason = f": {state.error}" if state.error else ""
            return AttemptStatus.LAUNCH_FAILED.value, f"the session failed{reason}"
        if now - state.updated_at < SESSION_SETTLE_SECONDS:
            return None
        if state.model_tokens == 0:
            # Quiet and its model never answered, whatever status it reports:
            # typically an agent that is not signed in to its provider.
            return (
                AttemptStatus.LAUNCH_FAILED.value,
                "the session never used its model; the agent may not be signed in "
                "or the model may not be available to it",
            )
        if state.pending_approvals > 0 or state.status == "waiting":
            return (
                AttemptStatus.LAUNCH_FAILED.value,
                "the session is waiting for an approval; give the rung a permission_mode "
                "or an agent that does not ask",
            )
        reached_repository = attempt.pull_request is not None or attempt.detail is not None
        if state.status == "idle" and not reached_repository:
            # The agent stopped without pushing: this rung did not solve it.
            return AttemptStatus.FAILED.value, "the session ended without pushing its branch"
        return None

    async def drain(self) -> None:
        """Wait for session launches started so far (tests and shutdown)."""
        while self._launches:
            await asyncio.gather(*list(self._launches), return_exceptions=True)

    async def _on_attempt_branch_event(self, event: CanonicalEvent, attempt_id: str) -> None:
        attempt = self._store.get_attempt_any_workspace(attempt_id)
        if attempt is None or event.fields.get("repo") != attempt.repo:
            return
        if event.kind == "push":
            self._store.note_progress(attempt.id, PROGRESS_BRANCH_PUSHED)
            return
        pull_request = event.fields.get("pull_request")
        if (
            event.kind == "pull_request.opened"
            and isinstance(pull_request, int)
            and attempt.pull_request is None
        ):
            self._store.set_pull_request(attempt.id, pull_request)
            return
        if event.kind not in VERIFY_KINDS or attempt.status != AttemptStatus.RUNNING.value:
            return
        ladder = self._store.get_ladder_any_workspace(attempt.ladder_id)
        if ladder is None or event.kind != ladder.verify_kind:
            return
        if ladder.verify_name is not None and event.fields.get("name") != ladder.verify_name:
            return
        conclusion = event.fields.get("conclusion")
        check = event.fields.get("name") or event.kind
        if conclusion in _PASSING_CONCLUSIONS:
            await self._finish(attempt, ladder, AttemptStatus.PASSED.value, f"{check} passed")
        elif conclusion in _FAILING_CONCLUSIONS:
            url = event.fields.get("url")
            detail = f"{check} concluded {conclusion}" + (f" ({url})" if url else "")
            await self._finish(attempt, ladder, AttemptStatus.FAILED.value, detail)

    async def _finish(
        self, attempt: LadderAttempt, ladder: Ladder | None, status: str, detail: str
    ) -> bool:
        """End an attempt once; escalate when it did not pass."""
        spend: SessionSpend | None = None
        if self._read_spend is not None and attempt.conversation_id is not None:
            try:
                spend = await asyncio.to_thread(self._read_spend, attempt)
            except Exception:
                _logger.exception("Could not read spend for ladder attempt %s", attempt.id)
        ended = self._store.finish_attempt(
            attempt.id,
            status=status,
            finished_at=int(self._clock()),
            detail=detail,
            cost_usd=spend.cost_usd if spend else None,
            model=spend.model if spend else None,
        )
        if not ended:
            return False
        _logger.info("Ladder attempt %s ended %s: %s", attempt.id, status, detail)
        next_index = attempt.rung_index + 1
        if (
            status in ESCALATING_STATUSES
            and ladder is not None
            and ladder.state == LadderState.ACTIVE.value
            and next_index < len(ladder.rungs)
        ):
            self._start_attempt(
                ladder,
                task_id=attempt.task_id,
                task_key=attempt.task_key,
                rung_index=next_index,
                event_fields=attempt.event_fields,
                kind=ladder.trigger_kind,
                previous=replace(attempt, status=status, detail=detail),
            )
        return True

    async def _start_tasks(self, event: CanonicalEvent) -> None:
        for ladder in self._store.list_active(event.kind):
            if event.fields.get("repo") != ladder.repo or not ladder.rungs:
                continue
            if not event_matches(ladder.trigger_match, event.fields):
                continue
            task_key = task_key_for(event)
            if self._store.has_running_attempt(ladder.id, task_key):
                continue
            self._start_attempt(
                ladder,
                task_id=uuid.uuid4().hex,
                task_key=task_key,
                rung_index=0,
                event_fields=event.fields,
                kind=event.kind,
                previous=None,
            )

    def _start_attempt(
        self,
        ladder: Ladder,
        *,
        task_id: str,
        task_key: str,
        rung_index: int,
        event_fields: dict[str, Any],
        kind: str,
        previous: LadderAttempt | None,
    ) -> None:
        rung = ladder.rungs[rung_index]
        attempt = self._store.create_attempt(
            uuid.uuid4().hex,
            ladder=ladder,
            task_id=task_id,
            task_key=task_key,
            rung_index=rung_index,
            event_fields=event_fields,
            started_at=int(self._clock()),
        )
        title = f"{ladder.name}: {task_key} (rung {rung_index + 1})"
        request = LaunchRequest(
            owner_user_id=ladder.user_id,
            agent_id=rung.agent_id,
            prompt=build_attempt_prompt(ladder, attempt, kind=kind, previous=previous),
            title=title[:_TITLE_MAX_CHARS],
            source_id=f"ladder:{ladder.id}",
            workspace_id=ladder.workspace_id,
            account_generation=ladder.account_generation,
            execution_target=ladder.execution_target,
            host_id=ladder.host_id,
            workspace=ladder.workspace,
            max_cost_usd=rung.max_cost_usd,
            model_override=rung.model_override,
            permission_mode=rung.permission_mode,
            labels={
                LAUNCHED_BY_LABEL: "ladder",
                LADDER_LABEL: ladder.id,
                LADDER_ATTEMPT_LABEL: attempt.id,
            },
        )
        # A launch can outlast a webhook delivery, so it runs in the background.
        task = asyncio.create_task(
            self._run_launch(attempt, request), name=f"ladder-launch-{attempt.id}"
        )
        self._launches.add(task)
        task.add_done_callback(self._launches.discard)

    async def _run_launch(self, attempt: LadderAttempt, request: LaunchRequest) -> None:
        try:
            outcome = await self._launch(request)
        except Exception as exc:
            _logger.exception("Launch failed for ladder attempt %s", attempt.id)
            outcome = LaunchOutcome(status="failed", error=str(exc))
        if outcome.status == "running" and outcome.conversation_id:
            self._store.set_conversation(attempt.id, outcome.conversation_id)
            return
        # A session that never started says nothing about the rung, so the
        # task stops here instead of moving up the ladder.
        self._store.finish_attempt(
            attempt.id,
            status=AttemptStatus.LAUNCH_FAILED.value,
            finished_at=int(self._clock()),
            detail=outcome.error or f"launch {outcome.status}",
        )
