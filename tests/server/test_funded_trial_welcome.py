"""Welcome grant: operator config parsing and the idempotent registration hook."""

from __future__ import annotations

import pytest

from omnigent.server.funded_trial import FundedTrialConfig, FundedTrialPolicy, WelcomeGrant
from omnigent.server.funded_trial_store import MICROS_PER_USD, FundedTrialStore

_MODELS = {
    "claude-sonnet-test": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
}


def test_config_without_welcome_grant_grants_nothing() -> None:
    assert FundedTrialConfig.from_mapping({"models": _MODELS}).welcome_grant is None
    assert FundedTrialConfig().welcome_grant is None


def test_welcome_grant_parses_terms_and_defaults_capabilities() -> None:
    config = FundedTrialConfig.from_mapping(
        {"models": _MODELS, "welcome_grant": {"allowance_usd": 3, "expires_in_days": 7}}
    )
    assert config.welcome_grant == WelcomeGrant(
        allowance_usd=3.0,
        expires_in_days=7.0,
        capabilities=frozenset({"inference", "compute", "coding", "pull_request"}),
        max_concurrent_roots=1,
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"allowance_usd": 0, "expires_in_days": 7},
        {"allowance_usd": 3, "expires_in_days": 0},
        {"allowance_usd": 3, "expires_in_days": 7, "capabilities": ["teleport"]},
        {"allowance_usd": 3, "expires_in_days": 7, "max_concurrent_roots": 0},
    ],
)
def test_welcome_grant_rejects_bad_terms(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WelcomeGrant.from_mapping(raw)


def test_grant_welcome_is_idempotent_and_respects_the_switch(db_uri: str) -> None:
    clock = lambda: 1_700_000_000.0  # noqa: E731
    store = FundedTrialStore(db_uri, clock=clock)
    config = FundedTrialConfig.from_mapping(
        {
            "models": _MODELS,
            "welcome_grant": {
                "allowance_usd": 3,
                "expires_in_days": 7,
                "capabilities": ["inference", "coding"],
            },
        }
    )
    policy = FundedTrialPolicy(enabled=True, store=store, config=config, clock=clock)

    grant = policy.grant_welcome("carol")
    assert grant is not None
    assert grant.allowance_micros == 3 * MICROS_PER_USD
    assert grant.expires_at == 1_700_000_000 + 7 * 86_400
    assert grant.capabilities == frozenset({"inference", "coding"})
    assert grant.kind == "trial"

    # A second registration (tombstone reuse) keeps the first ledger.
    assert policy.grant_welcome("carol") is None
    kept = store.get_grant("carol", "trial")
    assert kept is not None and kept.id == grant.id

    disabled = FundedTrialPolicy(enabled=False, store=store, config=config, clock=clock)
    assert disabled.grant_welcome("dave") is None
    unconfigured = FundedTrialPolicy(
        enabled=True,
        store=store,
        config=FundedTrialConfig.from_mapping({"models": _MODELS}),
        clock=clock,
    )
    assert unconfigured.grant_welcome("dave") is None
    assert policy.grant_welcome("") is None


def _policy(db_uri: str, clock: object, **welcome: object) -> FundedTrialPolicy:
    config = FundedTrialConfig.from_mapping(
        {
            "models": _MODELS,
            "welcome_grant": {"allowance_usd": 3, "expires_in_days": 7, **welcome},
        }
    )
    return FundedTrialPolicy(
        enabled=True, store=FundedTrialStore(db_uri, clock=clock), config=config, clock=clock
    )


def test_welcome_grant_caps_parse_and_reject_bad_values() -> None:
    terms = WelcomeGrant.from_mapping(
        {"allowance_usd": 3, "expires_in_days": 7, "max_per_day": 5, "max_per_source_per_week": 0}
    )
    assert (terms.max_per_day, terms.max_per_source_per_week) == (5, 0)
    defaults = WelcomeGrant.from_mapping({"allowance_usd": 3, "expires_in_days": 7})
    assert (defaults.max_per_day, defaults.max_per_source_per_week) == (20, 1)
    for bad in ({"max_per_day": 0}, {"max_per_source_per_week": -1}):
        with pytest.raises(ValueError):
            WelcomeGrant.from_mapping({"allowance_usd": 3, "expires_in_days": 7, **bad})


def test_daily_cap_stops_issuing_after_the_limit_and_resets(db_uri: str) -> None:
    now = [1_700_000_000.0]
    policy = _policy(db_uri, lambda: now[0], max_per_day=2, max_per_source_per_week=0)
    assert policy.grant_welcome("a1") is not None
    assert policy.grant_welcome("a2") is not None
    assert policy.grant_welcome("a3") is None  # third signup of the day: account, no credit
    now[0] += 86_400 + 1  # the rolling window has moved past the first two
    assert policy.grant_welcome("a4") is not None


def test_per_source_cap_counts_a_hashed_address_for_a_week(db_uri: str) -> None:
    now = [1_700_000_000.0]
    policy = _policy(db_uri, lambda: now[0], max_per_day=100, max_per_source_per_week=1)
    assert policy.grant_welcome("b1", source="hash-A") is not None
    assert policy.grant_welcome("b2", source="hash-A") is None  # same address, same week
    assert policy.grant_welcome("b3", source="hash-B") is not None  # another address is fine
    assert policy.grant_welcome("b4") is not None  # unknown source is not counted
    now[0] += 7 * 86_400 + 1
    assert policy.grant_welcome("b5", source="hash-A") is not None
