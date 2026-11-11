"""Unit tests for the ladder scoreboard aggregation and attempt branch names."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server.ladders.model import (
    Ladder,
    LadderAttempt,
    Rung,
    attempt_branch,
    attempt_id_from_branch,
)
from omnigent.server.ladders.scoreboard import build_scoreboard

_LADDER = Ladder(
    id="lad1",
    user_id="alice@example.com",
    name="issue fixer",
    repo="acme/api",
    trigger_kind="issues.opened",
    prompt_template="Fix it",
    rungs=(Rung(agent_id="ag_small", label="cheap"), Rung(agent_id="ag_big", label="strong")),
    created_at=1,
)


def _attempt(task: str, rung: int, status: str, cost: float | None, **extra: Any) -> LadderAttempt:
    return LadderAttempt(
        id=f"{task}-{rung}",
        ladder_id="lad1",
        task_id=task,
        user_id="alice@example.com",
        repo="acme/api",
        task_key=f"issue:{task}",
        rung_index=rung,
        agent_id="ag_small" if rung == 0 else "ag_big",
        status=status,
        started_at=10,
        cost_usd=cost,
        **extra,
    )


def test_scoreboard_totals_tasks_and_scores_each_rung() -> None:
    attempts = [
        _attempt("t1", 0, "passed", 0.10, model="haiku"),
        _attempt("t2", 0, "failed", 0.20, model="haiku"),
        _attempt("t2", 1, "passed", 1.00, model="opus"),
        _attempt("t3", 0, "timed_out", 0.30),
        _attempt("t3", 1, "failed", 2.00),
        _attempt("t4", 0, "running", None),
        _attempt("t5", 0, "launch_failed", None),
    ]

    board = build_scoreboard(attempts, {"lad1": _LADDER}, window_days=30, repo="acme/api")

    assert board["window_days"] == 30 and board["repo"] == "acme/api"
    assert board["tasks"] == {
        "total": 5,
        "solved": 2,
        "unsolved": 2,
        "in_progress": 1,
        "cost_usd": pytest.approx(3.60),
        "cost_per_solved_task": pytest.approx(1.80),
        "solved_by_rung": [1, 1],
    }
    cheap, strong = board["rungs"]
    assert (cheap["rung_index"], cheap["label"], cheap["ladder_name"]) == (
        0,
        "cheap",
        "issue fixer",
    )
    assert (cheap["attempts"], cheap["passed"], cheap["failed"], cheap["timed_out"]) == (
        5,
        1,
        1,
        1,
    )
    assert (cheap["running"], cheap["launch_failed"]) == (1, 1)
    # Launch failures and running attempts have no verdict: 1 pass of 3 decided.
    assert cheap["pass_rate"] == pytest.approx(1 / 3)
    assert cheap["cost_usd"] == pytest.approx(0.60)
    assert cheap["cost_per_pass"] == pytest.approx(0.60)
    assert cheap["model"] == "haiku"
    assert (strong["label"], strong["attempts"], strong["pass_rate"]) == ("strong", 2, 0.5)
    assert strong["cost_per_pass"] == pytest.approx(3.00)
    assert cheap["low_yield"] is False and strong["low_yield"] is False


def test_scoreboard_flags_a_rung_that_rarely_passes_once_it_has_enough_verdicts() -> None:
    nine = [_attempt(f"t{n}", 0, "failed", 0.1) for n in range(9)]
    assert build_scoreboard(nine, {}, window_days=7, repo=None)["rungs"][0]["low_yield"] is False

    ten = [*nine, _attempt("t9", 0, "passed", 0.1)]
    row = build_scoreboard(ten, {}, window_days=7, repo=None)["rungs"][0]
    assert row["pass_rate"] == pytest.approx(0.1)
    assert row["low_yield"] is True
    # An unknown ladder still gets a row, without a name or label.
    assert row["ladder_name"] is None and row["label"] is None


def test_scoreboard_with_no_attempts_has_no_ratios() -> None:
    board = build_scoreboard([], {}, window_days=30, repo=None)
    assert board["rungs"] == []
    assert board["tasks"]["total"] == 0
    assert board["tasks"]["cost_per_solved_task"] is None
    assert board["tasks"]["solved_by_rung"] == []


@pytest.mark.parametrize(
    "branch",
    ["main", "wakevane/att-", "wakevane/att-xyz", "wakevane/att-" + "g" * 32, None, 7],
)
def test_other_branches_are_not_attempt_branches(branch: object) -> None:
    assert attempt_id_from_branch(branch) is None


def test_attempt_branch_round_trips() -> None:
    attempt_id = "0123456789abcdef0123456789abcdef"
    assert attempt_id_from_branch(attempt_branch(attempt_id)) == attempt_id
