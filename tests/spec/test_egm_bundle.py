"""Validate the portable EGM bundle and its configured policy behavior offline."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from omnigent.inner.os_env import _assert_write_allowed
from omnigent.inner.sandbox import resolve_sandbox
from omnigent.spec import load, materialize_bundle


def test_egm_materialized_bundle_and_policies(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "examples" / "egm"
    bundle = materialize_bundle(source, tmp_path / "bundle")
    spec = load(bundle, enforce_handler_allowlist=True)
    assert spec.tools.agents == ["goldfish"]
    assert len(spec.sub_agents) == 1
    reviewer = spec.sub_agents[0]
    assert reviewer.name == "goldfish"
    assert not reviewer.tools.agents
    assert (bundle / "README.md").is_file()

    repo = tmp_path / "repo"
    output = repo / "docs" / "designs" / "egm"
    output.mkdir(parents=True)
    coordinator_policy = resolve_sandbox(spec.os_env, repo)
    reviewer_policy = resolve_sandbox(reviewer.os_env, repo)
    _assert_write_allowed(coordinator_policy, output / "run" / "r1.md")
    for policy, path in (
        (coordinator_policy, repo / "app.py"),
        (coordinator_policy, repo / "docs" / "other.md"),
        (reviewer_policy, output / "run" / "r1.md"),
    ):
        with pytest.raises(PermissionError):
            _assert_write_allowed(policy, path)

    for agent in (spec, reviewer):
        assert agent.guardrails is not None
        policies = {}
        for policy in agent.guardrails.policies:
            module, _, name = policy.function.path.rpartition(".")
            factory = getattr(importlib.import_module(module), name)
            policies[policy.name] = factory(**(policy.function.arguments or {}))
        budget = policies["budget"]
        usage_key = "subtree_usage" if agent is reviewer else "usage"
        for phase in ("request", "tool_call"):
            event = {
                "type": phase,
                "context": {usage_key: {"total_cost_usd": 100.0}, "model": "test-model"},
            }
            assert budget(event)["result"] == ("ASK" if agent is reviewer else "DENY")
            event["context"][usage_key]["total_cost_usd"] = 0.01
            if agent is reviewer:
                # Prior coordinator/sibling spend must not exhaust a fresh review.
                event["context"]["usage"] = {"total_cost_usd": 2.13}
            assert budget(event)["result"] == "ALLOW"
        if agent is reviewer:
            event = {
                "type": "tool_call",
                "data": {"name": "sys_os_write", "arguments": {"path": "app.py"}},
            }
            assert policies["read_only_os"](event, {})["result"] == "DENY"
