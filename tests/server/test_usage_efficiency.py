"""Unit tests for the efficiency scorecard math in ``omnigent.server.usage_efficiency``."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server.usage_efficiency import (
    UNKNOWN_HARNESS,
    TokenCounters,
    aggregate,
    counters_from_usage,
    efficiency_from_usage,
    model_counters_from_usage,
    score_session,
    session_cost_usd,
)

# ── efficiency_from_usage ─────────────────────────────────────────────


def test_efficiency_from_usage_normal() -> None:
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 100,
        "output_tokens": 125,
        "total_cost_usd": 2.5,
    }
    metrics = efficiency_from_usage(usage)
    # prompt = 100 + 300 + 100 = 500; cache hit = 300 / 500; output = 125 / 625.
    assert metrics.prompt_tokens == 500
    assert metrics.output_tokens == 125
    assert metrics.cache_hit_rate == pytest.approx(0.6)
    assert metrics.output_share == pytest.approx(0.2)
    assert metrics.cost_usd == 2.5


def test_efficiency_from_usage_zero_denominators() -> None:
    # No prompt tokens: cache hit rate is undefined, output share is 1.0.
    output_only = efficiency_from_usage({"output_tokens": 10})
    assert output_only.cache_hit_rate is None
    assert output_only.output_share == pytest.approx(1.0)
    # No tokens at all: both rates undefined, never a ZeroDivisionError.
    empty = efficiency_from_usage({})
    assert empty.cache_hit_rate is None
    assert empty.output_share is None
    assert empty.prompt_tokens == 0
    assert empty.output_tokens == 0
    assert empty.cost_usd == 0.0


def test_efficiency_from_usage_missing_and_malformed_keys() -> None:
    usage: dict[str, Any] = {
        "input_tokens": "not-a-number",
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": True,  # bool is not a counter
        "output_tokens": 40.0,  # subtree sums are floats
        "total_cost_usd": "oops",
    }
    metrics = efficiency_from_usage(usage)
    assert metrics.prompt_tokens == 0
    assert metrics.output_tokens == 40
    assert metrics.cache_hit_rate is None
    assert metrics.output_share == pytest.approx(1.0)
    assert metrics.cost_usd == 0.0


def test_efficiency_from_usage_by_model_fallback() -> None:
    # No top-level counters: each is summed across the per-model buckets.
    usage = {
        "total_cost_usd": 3.0,
        "by_model": {
            "model-a": {"input_tokens": 50, "cache_read_input_tokens": 150, "output_tokens": 20},
            "model-b": {"input_tokens": 50, "cache_read_input_tokens": 50, "output_tokens": 30},
            "broken": "not-a-bucket",
        },
    }
    metrics = efficiency_from_usage(usage)
    assert metrics.prompt_tokens == 300
    assert metrics.output_tokens == 50
    assert metrics.cache_hit_rate == pytest.approx(200 / 300)
    assert metrics.output_share == pytest.approx(50 / 350)
    assert metrics.cost_usd == 3.0


def test_counters_from_usage_top_level_wins_per_key() -> None:
    # A present top-level key (even 0) wins; only an absent key defers to by_model.
    usage = {
        "input_tokens": 0,
        "by_model": {"m": {"input_tokens": 999, "output_tokens": 7}},
    }
    counters = counters_from_usage(usage)
    assert counters.input_tokens == 0
    assert counters.output_tokens == 7


def test_counters_from_usage_cost_never_falls_back_to_by_model() -> None:
    # Per-model costs are snapshots, so an unpriced session stays 0.0.
    usage = {"by_model": {"m": {"total_cost_usd": 9.0}}}
    assert counters_from_usage(usage).cost_usd == 0.0
    assert session_cost_usd(usage) == 0.0
    assert session_cost_usd({"total_cost_usd": 1.25}) == 1.25


def test_token_counters_add() -> None:
    total = TokenCounters(1, 2, 3, 4, 0.5) + TokenCounters(10, 20, 30, 40, 1.0)
    assert total == TokenCounters(11, 22, 33, 44, 1.5)


# ── aggregate ─────────────────────────────────────────────────────────


def _scored(
    session_id: str,
    *,
    cost: float,
    harness: str | None,
    by_model: dict[str, dict[str, float]],
    created_at: int = 1_700_000_000,
) -> Any:
    counters: dict[str, Any] = {"total_cost_usd": cost, "by_model": by_model}
    return score_session(
        counters, session_id=session_id, created_at=created_at, harness=harness, title=session_id
    )


def _bucket(*, input_tokens: int, cache_read: int, output: int, cost: float) -> dict[str, float]:
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
        "total_cost_usd": cost,
    }


def test_aggregate_sorts_by_cost_and_truncates() -> None:
    sessions = [
        _scored(
            "cheap",
            cost=1.0,
            harness="h",
            by_model={"m": _bucket(input_tokens=1, cache_read=0, output=1, cost=1.0)},
        ),
        _scored(
            "dear",
            cost=9.0,
            harness="h",
            by_model={"m": _bucket(input_tokens=1, cache_read=0, output=1, cost=9.0)},
        ),
        _scored(
            "mid",
            cost=5.0,
            harness="h",
            by_model={"m": _bucket(input_tokens=1, cache_read=0, output=1, cost=5.0)},
        ),
    ]
    report = aggregate(sessions, window_days=7, limit=2)
    assert report.window_days == 7
    assert [row.id for row in report.sessions] == ["dear", "mid"]
    # Totals cover every session, not just the truncated rows.
    assert report.totals.sessions == 3
    assert report.totals.cost_usd == pytest.approx(15.0)
    assert report.totals.cost_per_session_usd == pytest.approx(5.0)
    assert report.by_harness["h"].sessions == 3


def test_aggregate_cost_share_sums_to_one_and_rates_per_model() -> None:
    sessions = [
        _scored(
            "s1",
            cost=4.0,
            harness="claude-native",
            by_model={
                "opus": _bucket(input_tokens=100, cache_read=300, output=50, cost=3.0),
                "haiku": _bucket(input_tokens=100, cache_read=0, output=10, cost=1.0),
            },
        ),
        _scored(
            "s2",
            cost=2.0,
            harness="claude-native",
            by_model={"opus": _bucket(input_tokens=0, cache_read=400, output=50, cost=2.0)},
        ),
    ]
    report = aggregate(sessions, window_days=30, limit=50)
    assert set(report.by_model) == {"opus", "haiku"}
    assert sum(m.cost_share or 0.0 for m in report.by_model.values()) == pytest.approx(1.0)
    assert report.by_model["opus"].cost_share == pytest.approx(5 / 6)
    assert report.by_model["haiku"].cost_share == pytest.approx(1 / 6)
    # opus across both sessions: prompt 100+300+0+400 = 800, cache 700.
    assert report.by_model["opus"].prompt_tokens == 800
    assert report.by_model["opus"].cache_hit_rate == pytest.approx(700 / 800)
    assert report.by_model["opus"].output_share == pytest.approx(100 / 900)
    assert report.by_model["haiku"].cache_hit_rate == pytest.approx(0.0)
    # Session rows list models dominant-cost first.
    assert report.sessions[0].id == "s1"
    assert report.sessions[0].model_ids == ["opus", "haiku"]


def test_aggregate_groups_by_harness_with_unknown_bucket() -> None:
    sessions = [
        _scored(
            "a",
            cost=1.0,
            harness="claude-native",
            by_model={"m": _bucket(input_tokens=100, cache_read=100, output=10, cost=1.0)},
        ),
        _scored(
            "b",
            cost=2.0,
            harness="claude-native",
            by_model={"m": _bucket(input_tokens=100, cache_read=300, output=10, cost=2.0)},
        ),
        _scored(
            "c",
            cost=4.0,
            harness="codex-native",
            by_model={"m": _bucket(input_tokens=100, cache_read=0, output=10, cost=4.0)},
        ),
        _scored("d", cost=0.0, harness=None, by_model={}),
    ]
    report = aggregate(sessions, window_days=30, limit=50)
    assert set(report.by_harness) == {"claude-native", "codex-native", UNKNOWN_HARNESS}
    claude = report.by_harness["claude-native"]
    assert claude.sessions == 2
    assert claude.cost_usd == pytest.approx(3.0)
    assert claude.cost_per_session_usd == pytest.approx(1.5)
    # Rates come from summed counters: cache 400 / prompt 600.
    assert claude.cache_hit_rate == pytest.approx(400 / 600)
    assert report.by_harness["codex-native"].cache_hit_rate == pytest.approx(0.0)
    unknown = report.by_harness[UNKNOWN_HARNESS]
    assert unknown.sessions == 1
    assert unknown.cache_hit_rate is None
    assert report.totals.sessions == 4
    assert report.totals.cost_usd == pytest.approx(7.0)


def test_aggregate_empty() -> None:
    report = aggregate([], window_days=30, limit=10)
    assert report.sessions == []
    assert report.by_harness == {}
    assert report.by_model == {}
    assert report.totals.sessions == 0
    assert report.totals.cost_usd == 0.0
    assert report.totals.cost_per_session_usd is None
    assert report.totals.cache_hit_rate is None


def test_aggregate_cost_share_none_when_no_model_cost() -> None:
    sessions = [
        _scored(
            "a", cost=0.0, harness="h", by_model={"m": {"input_tokens": 10, "output_tokens": 1}}
        ),
    ]
    report = aggregate(sessions, window_days=1, limit=1)
    assert report.by_model["m"].cost_share is None
    assert report.by_model["m"].output_share == pytest.approx(1 / 11)


def test_model_counters_merge_dated_claude_snapshot_into_alias() -> None:
    usage = {
        "by_model": {
            "claude-haiku-4-5-20251001": {"input_tokens": 10, "output_tokens": 1},
            "claude-haiku-4-5": {"input_tokens": 5, "output_tokens": 2},
            "gpt-5-20251001": {"input_tokens": 3},
        }
    }
    counters = model_counters_from_usage(usage)
    assert set(counters) == {"claude-haiku-4-5", "gpt-5-20251001"}
    assert counters["claude-haiku-4-5"].input_tokens == 15
    assert counters["claude-haiku-4-5"].output_tokens == 3
