"""Pure aggregation behind ``GET /v1/ladders/scoreboard``.

Rung rows are per position in a ladder. The first rung sees every task; a
later rung only sees tasks the rungs before it failed, so its pass rate
describes that position in that ladder, not the agent in general.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from omnigent.server.ladders.model import AttemptStatus, Ladder, LadderAttempt

# A rung is flagged once this many attempts have a verdict …
LOW_YIELD_MIN_DECIDED = 10
# … and fewer than this share of them passed.
LOW_YIELD_MAX_PASS_RATE = 0.2

_NOT_PASSED = frozenset({AttemptStatus.FAILED.value, AttemptStatus.TIMED_OUT.value})


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _rung_row(
    ladder_id: str, rung_index: int, attempts: list[LadderAttempt], ladder: Ladder | None
) -> dict[str, Any]:
    statuses = Counter(attempt.status for attempt in attempts)
    passed = statuses[AttemptStatus.PASSED.value]
    not_passed = sum(statuses[status] for status in _NOT_PASSED)
    decided = passed + not_passed
    cost = sum(attempt.cost_usd or 0.0 for attempt in attempts)
    pass_rate = _ratio(passed, decided)
    models = Counter(attempt.model for attempt in attempts if attempt.model)
    rung = ladder.rungs[rung_index] if ladder and rung_index < len(ladder.rungs) else None
    first = attempts[0]
    return {
        "ladder_id": ladder_id,
        "ladder_name": ladder.name if ladder else None,
        "repo": first.repo,
        "rung_index": rung_index,
        "label": rung.label if rung else None,
        "agent_id": first.agent_id,
        "model_override": first.model_override,
        "model": models.most_common(1)[0][0] if models else None,
        "attempts": len(attempts),
        "passed": passed,
        "failed": statuses[AttemptStatus.FAILED.value],
        "timed_out": statuses[AttemptStatus.TIMED_OUT.value],
        "running": statuses[AttemptStatus.RUNNING.value],
        "launch_failed": statuses[AttemptStatus.LAUNCH_FAILED.value],
        "pass_rate": pass_rate,
        "cost_usd": cost,
        "cost_per_attempt": _ratio(cost, len(attempts)),
        "cost_per_pass": _ratio(cost, passed),
        "low_yield": (
            decided >= LOW_YIELD_MIN_DECIDED
            and pass_rate is not None
            and pass_rate < LOW_YIELD_MAX_PASS_RATE
        ),
    }


def _task_totals(attempts: Iterable[LadderAttempt]) -> dict[str, Any]:
    by_task: dict[str, list[LadderAttempt]] = defaultdict(list)
    for attempt in attempts:
        by_task[attempt.task_id].append(attempt)
    solved = in_progress = 0
    cost = 0.0
    solved_by_rung: Counter[int] = Counter()
    for task_attempts in by_task.values():
        cost += sum(attempt.cost_usd or 0.0 for attempt in task_attempts)
        winner = next((a for a in task_attempts if a.status == AttemptStatus.PASSED.value), None)
        if winner is not None:
            solved += 1
            solved_by_rung[winner.rung_index] += 1
        elif any(a.status == AttemptStatus.RUNNING.value for a in task_attempts):
            in_progress += 1
    highest_rung = max(solved_by_rung, default=-1)
    return {
        "total": len(by_task),
        "solved": solved,
        "unsolved": len(by_task) - solved - in_progress,
        "in_progress": in_progress,
        "cost_usd": cost,
        "cost_per_solved_task": _ratio(cost, solved),
        "solved_by_rung": [solved_by_rung[index] for index in range(highest_rung + 1)],
    }


def build_scoreboard(
    attempts: Iterable[LadderAttempt],
    ladders: Mapping[str, Ladder],
    *,
    window_days: int,
    repo: str | None,
) -> dict[str, Any]:
    """
    Summarize attempts into task totals and one row per ladder rung.

    :param attempts: Attempts in the window, each carrying its latest cost.
    :param ladders: The caller's ladders by id, for names and rung labels.
    :param window_days: Window the attempts were read for, echoed back.
    :param repo: Repository filter that was applied, e.g. ``"acme/api"``.
    :returns: The scoreboard document.
    """
    attempts = list(attempts)
    by_rung: dict[tuple[str, int], list[LadderAttempt]] = defaultdict(list)
    for attempt in attempts:
        by_rung[(attempt.ladder_id, attempt.rung_index)].append(attempt)
    rows = [
        _rung_row(ladder_id, rung_index, rung_attempts, ladders.get(ladder_id))
        for (ladder_id, rung_index), rung_attempts in sorted(by_rung.items())
    ]
    return {
        "window_days": window_days,
        "repo": repo,
        "tasks": _task_totals(attempts),
        "rungs": rows,
    }
