"""
Pure computation for the efficiency scorecard (``GET /v1/usage/efficiency``).

Turns the per-session usage dicts that
:func:`omnigent.runtime.policies.builder.load_session_usage_and_tree` returns
into the rates described in ``designs/HARNESS_FLYWHEEL.md`` section 4:

- ``prompt_tokens = input + cache_read + cache_creation``
- ``cache_hit_rate = cache_read / prompt_tokens``
- ``output_share = output / (prompt_tokens + output)``

No store or server dependencies: the route in
:mod:`omnigent.server.routes.usage` walks the sessions and hands each usage
dict here, so every formula is unit-testable from a plain dict.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.server.schemas import (
    EfficiencyMetrics,
    EfficiencyReport,
    EfficiencyTotals,
    ModelEfficiency,
    SessionEfficiency,
)

# Harness bucket for sessions whose harness could not be resolved.
UNKNOWN_HARNESS = "unknown"

# Token counter keys read from a usage dict or one of its ``by_model`` buckets.
_TOKEN_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _coerce_int(value: object) -> int:
    """
    Read a token counter defensively: missing, ``None``, bools and
    non-numeric values all count as ``0``.

    Persisted usage is harness-reported JSON, so a counter may be absent,
    a float (subtree sums are accumulated as floats), or malformed.

    :param value: The raw counter value.
    :returns: The counter as a non-negative int.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def session_cost_usd(usage: Mapping[str, Any]) -> float:
    """
    Read a session's authoritative cumulative cost, or ``0.0`` when unpriced.

    ``total_cost_usd`` is present only on priced sessions (the "priced ⟺ key
    present" contract); an absent or malformed value reads as ``0.0``.

    :param usage: A subtree-summed ``session_usage`` dict (or one of its
        ``by_model`` buckets).
    :returns: The recorded cost in USD.
    """
    if "total_cost_usd" not in usage:
        return 0.0
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class TokenCounters:
    """
    The four token counters plus cost that every efficiency rate derives
    from. Immutable and additive so one type serves a session, a model, a
    harness and the whole window.

    :param input_tokens: Uncached prompt tokens.
    :param cache_read_tokens: Prompt tokens served from the prompt cache.
    :param cache_creation_tokens: Prompt tokens written to the prompt cache.
    :param output_tokens: Generated tokens.
    :param cost_usd: Recorded USD spend for these counters.
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: TokenCounters) -> TokenCounters:
        return TokenCounters(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    @property
    def prompt_tokens(self) -> int:
        """``input + cache_read + cache_creation``: everything the model read."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """``cache_read / prompt_tokens``, or ``None`` with no prompt tokens."""
        if self.prompt_tokens == 0:
            return None
        return self.cache_read_tokens / self.prompt_tokens

    @property
    def output_share(self) -> float | None:
        """``output / (prompt_tokens + output)``, or ``None`` with no tokens."""
        total = self.prompt_tokens + self.output_tokens
        if total == 0:
            return None
        return self.output_tokens / total

    def to_metrics(self) -> EfficiencyMetrics:
        """Project the counters into the API's :class:`EfficiencyMetrics`."""
        return EfficiencyMetrics(
            cost_usd=self.cost_usd,
            prompt_tokens=self.prompt_tokens,
            output_tokens=self.output_tokens,
            cache_hit_rate=self.cache_hit_rate,
            output_share=self.output_share,
        )

    def to_totals(self, sessions: int) -> EfficiencyTotals:
        """
        Project the counters into :class:`EfficiencyTotals` for a group.

        :param sessions: Number of sessions summed into these counters.
        :returns: The group row, with ``cost_per_session_usd`` ``None`` for
            an empty group.
        """
        return EfficiencyTotals(
            cost_usd=self.cost_usd,
            prompt_tokens=self.prompt_tokens,
            output_tokens=self.output_tokens,
            cache_hit_rate=self.cache_hit_rate,
            output_share=self.output_share,
            sessions=sessions,
            cost_per_session_usd=self.cost_usd / sessions if sessions else None,
        )


def counters_from_bucket(bucket: Mapping[str, Any]) -> TokenCounters:
    """
    Read one usage bucket (a ``by_model`` entry) into :class:`TokenCounters`.

    :param bucket: A per-model dict such as
        ``{"input_tokens": 100, "cache_read_input_tokens": 25, ...}``.
    :returns: The bucket's counters; missing keys read as ``0``.
    """
    return TokenCounters(
        input_tokens=_coerce_int(bucket.get("input_tokens")),
        cache_read_tokens=_coerce_int(bucket.get("cache_read_input_tokens")),
        cache_creation_tokens=_coerce_int(bucket.get("cache_creation_input_tokens")),
        output_tokens=_coerce_int(bucket.get("output_tokens")),
        cost_usd=session_cost_usd(bucket),
    )


# A dated Claude snapshot id, e.g. ``claude-haiku-4-5-20251001``.
_CLAUDE_SNAPSHOT_ID_RE = re.compile(r"(claude-.+)-\d{8}")


def scorecard_model_id(model_id: str) -> str:
    """
    Name a model the way the scorecard groups it.

    Harnesses report the same Claude model under its alias or its dated
    snapshot id; the scorecard counts both as the alias.

    :param model_id: A reported id, e.g. ``"claude-haiku-4-5-20251001"``.
    :returns: The alias (``"claude-haiku-4-5"``), or *model_id* unchanged.
    """
    snapshot = _CLAUDE_SNAPSHOT_ID_RE.fullmatch(model_id)
    return snapshot.group(1) if snapshot else model_id


def model_counters_from_usage(usage: Mapping[str, Any]) -> dict[str, TokenCounters]:
    """
    Read every well-formed ``by_model`` bucket of a usage dict.

    :param usage: A subtree-summed ``session_usage`` dict.
    :returns: ``{model_id: counters}`` keyed by :func:`scorecard_model_id`;
        empty when there is no per-model breakdown. Non-dict buckets
        (malformed persisted data) are skipped.
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, Mapping):
        return {}
    merged: dict[str, TokenCounters] = defaultdict(TokenCounters)
    for model_id, bucket in by_model.items():
        if isinstance(bucket, Mapping):
            merged[scorecard_model_id(str(model_id))] += counters_from_bucket(bucket)
    return dict(merged)


def counters_from_usage(usage: Mapping[str, Any]) -> TokenCounters:
    """
    Read a session's summed token counters and authoritative cost.

    Each token counter is taken from the top-level key when present and
    otherwise summed across ``by_model`` (a harness may post only the
    per-model breakdown). Cost is the top-level ``total_cost_usd`` only:
    per-model costs are snapshots that are not guaranteed to sum to it.

    :param usage: A subtree-summed ``session_usage`` dict, possibly empty.
    :returns: The session's counters.
    """
    per_model = model_counters_from_usage(usage)
    model_sum = sum(per_model.values(), TokenCounters())
    top_level = counters_from_bucket(usage)

    def pick(key: str, top_value: int, fallback: int) -> int:
        # Top-level wins when the key exists at all (even as 0); only an
        # absent key defers to the by_model sum.
        return top_value if key in usage else fallback

    return TokenCounters(
        input_tokens=pick("input_tokens", top_level.input_tokens, model_sum.input_tokens),
        cache_read_tokens=pick(
            "cache_read_input_tokens", top_level.cache_read_tokens, model_sum.cache_read_tokens
        ),
        cache_creation_tokens=pick(
            "cache_creation_input_tokens",
            top_level.cache_creation_tokens,
            model_sum.cache_creation_tokens,
        ),
        output_tokens=pick("output_tokens", top_level.output_tokens, model_sum.output_tokens),
        cost_usd=session_cost_usd(usage),
    )


def efficiency_from_usage(usage: Mapping[str, Any]) -> EfficiencyMetrics:
    """
    Compute one session's efficiency metrics from its usage dict.

    :param usage: A subtree-summed ``session_usage`` dict (see
        :func:`omnigent.runtime.policies.builder.load_session_usage`).
        Counters may be missing or malformed; they read as ``0``, and
        top-level counters fall back to the ``by_model`` sum when absent.
    :returns: The metrics; rates are ``None`` when their denominator is 0.
    """
    return counters_from_usage(usage).to_metrics()


@dataclass(frozen=True)
class ScoredSession:
    """
    One session's scorecard row together with the per-model counters the
    report's ``by_model`` roll-up needs (the row itself carries only ids).

    :param row: The API row for this session.
    :param counters: The session's summed counters.
    :param by_model: Per-model counters for this session.
    """

    row: SessionEfficiency
    counters: TokenCounters
    by_model: dict[str, TokenCounters]


def score_session(
    usage: Mapping[str, Any],
    *,
    session_id: str,
    created_at: int,
    title: str | None = None,
    harness: str | None = None,
) -> ScoredSession:
    """
    Score one session from its usage dict and display metadata.

    :param usage: A subtree-summed ``session_usage`` dict.
    :param session_id: Conversation id, e.g. ``"conv_abc123"``.
    :param created_at: Unix epoch seconds of creation.
    :param title: Optional session title.
    :param harness: Best-effort harness name, or ``None`` if unresolved.
    :returns: The scored session.
    """
    counters = counters_from_usage(usage)
    by_model = model_counters_from_usage(usage)
    # Dominant model first so the biggest spend leads, matching ``omni usage``.
    model_ids = sorted(by_model, key=lambda model_id: by_model[model_id].cost_usd, reverse=True)
    row = SessionEfficiency(
        id=session_id,
        title=title,
        harness=harness,
        model_ids=model_ids,
        cost_usd=counters.cost_usd,
        cache_hit_rate=counters.cache_hit_rate,
        output_share=counters.output_share,
        prompt_tokens=counters.prompt_tokens,
        output_tokens=counters.output_tokens,
        created_at=created_at,
    )
    return ScoredSession(row=row, counters=counters, by_model=by_model)


def aggregate(
    sessions: Iterable[ScoredSession],
    *,
    window_days: int,
    limit: int,
) -> EfficiencyReport:
    """
    Roll scored sessions up into an :class:`EfficiencyReport`.

    ``totals``, ``by_harness`` and ``by_model`` cover every session given;
    only the ``sessions`` list is sorted by cost descending and truncated.

    :param sessions: Every scored session in the window.
    :param window_days: Reported as-is on the result.
    :param limit: Maximum number of session rows to return.
    :returns: The populated report.
    """
    scored = list(sessions)

    window = TokenCounters()
    harness_counters: dict[str, TokenCounters] = defaultdict(TokenCounters)
    harness_sessions: dict[str, int] = defaultdict(int)
    model_counters: dict[str, TokenCounters] = defaultdict(TokenCounters)
    for item in scored:
        window = window + item.counters
        harness = item.row.harness or UNKNOWN_HARNESS
        harness_counters[harness] = harness_counters[harness] + item.counters
        harness_sessions[harness] += 1
        for model_id, counters in item.by_model.items():
            model_counters[model_id] = model_counters[model_id] + counters

    # Shares are taken over the per-model cost sum, not the authoritative
    # window cost, so they always form a mix that adds up to 1.0.
    model_cost_total = sum(counters.cost_usd for counters in model_counters.values())
    by_model = {
        model_id: ModelEfficiency(
            **counters.to_metrics().model_dump(),
            cost_share=counters.cost_usd / model_cost_total if model_cost_total > 0 else None,
        )
        for model_id, counters in sorted(model_counters.items())
    }
    by_harness = {
        harness: counters.to_totals(harness_sessions[harness])
        for harness, counters in sorted(harness_counters.items())
    }
    ranked = sorted(scored, key=lambda item: item.row.cost_usd, reverse=True)
    return EfficiencyReport(
        window_days=window_days,
        totals=window.to_totals(len(scored)),
        by_harness=by_harness,
        by_model=by_model,
        sessions=[item.row for item in ranked[:limit]],
    )
