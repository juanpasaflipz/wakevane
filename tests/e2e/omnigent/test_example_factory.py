"""Structural test for the software-factory bundle (examples/factory).

The factory foreman coordinates three workers (``triager``, ``fixer``,
``watcher``) and is driven by events rather than polling. Pure spec-load — no
LLM, no credentials — modeled on ``test_example_sentinel.py``.

What breaks if this fails:
- a worker is dropped or renamed (the foreman loses triage, fixing or watching),
- the cheap workers lose their low reasoning effort (the efficiency lever disappears),
- the foreman or a read-only worker loses ``read_only_os`` (coordination stops
  being enforced at the policy layer),
- the foreman stops gating pushes (merges no longer ask a human),
- a cost budget is dropped from any agent in the tree.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_factory.py -> repo root is 3 parents up.
_FACTORY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "factory"


@pytest.fixture(scope="module")
def factory_spec() -> AgentSpec:
    """Load and validate the factory bundle once for the module."""
    return load(_FACTORY_BUNDLE)


def _policy_paths(spec: AgentSpec) -> dict[str, str]:
    assert spec.guardrails is not None
    return {policy.name: policy.function.path for policy in spec.guardrails.policies}


def _policy_args(spec: AgentSpec, name: str) -> dict[str, object]:
    assert spec.guardrails is not None
    policy = next(p for p in spec.guardrails.policies if p.name == name)
    return dict(policy.function.arguments or {})


def test_factory_name_and_workers(factory_spec: AgentSpec) -> None:
    assert factory_spec.name == "factory"
    assert sorted(factory_spec.tools.agents) == ["fixer", "triager", "watcher"]
    harness = {a.name: a.executor.config.get("harness") for a in factory_spec.sub_agents}
    assert harness == {"fixer": "claude-sdk", "triager": "claude-sdk", "watcher": "claude-sdk"}


def test_factory_effort_mix(factory_spec: AgentSpec) -> None:
    by_name = {a.name: a for a in factory_spec.sub_agents}
    assert by_name["triager"].executor.reasoning_effort == "low"
    assert by_name["watcher"].executor.reasoning_effort == "low"
    # The fixer is the one place full effort earns its cost.
    assert by_name["fixer"].executor.reasoning_effort is None
    # No agent pins a model id; each resolves from the configured provider.
    for spec in (factory_spec, *factory_spec.sub_agents):
        assert spec.executor.model is None, spec.name


def test_factory_foreman_asks_before_pushing(factory_spec: AgentSpec) -> None:
    paths = _policy_paths(factory_spec)
    assert paths["blast_radius"].endswith("orchestration.blast_radius")
    # Sub-agents run under the foreman's policies too; the fixer is exempt from
    # the push gate (pushing its branch is its job) and from the write deny.
    assert _policy_args(factory_spec, "blast_radius") == {
        "gate_pushes": True,
        "ungated_sub_agents": ["fixer"],
    }
    assert paths["read_only_os"].endswith("orchestration.read_only_os")
    assert _policy_args(factory_spec, "read_only_os") == {"allow_sub_agents": ["fixer"]}
    assert _policy_args(factory_spec, "purpose_guard") == {
        "allowed_purposes": ["explore", "implement", "review"]
    }


def test_factory_every_agent_has_a_cost_budget(factory_spec: AgentSpec) -> None:
    for spec in (factory_spec, *factory_spec.sub_agents):
        paths = _policy_paths(spec)
        budget_names = [name for name, path in paths.items() if path.endswith("cost.cost_budget")]
        assert budget_names, spec.name
        assert _policy_args(spec, budget_names[0]).get("max_cost_usd", 0) > 0, spec.name


def test_factory_read_only_workers(factory_spec: AgentSpec) -> None:
    by_name = {a.name: a for a in factory_spec.sub_agents}
    for name in ("triager", "watcher"):
        assert "read_only_os" in _policy_paths(by_name[name]), name
    # The fixer must be able to write; it is the only worker without the deny.
    assert "read_only_os" not in _policy_paths(by_name["fixer"])


def test_factory_policy_paths_importable(factory_spec: AgentSpec) -> None:
    for agent in (factory_spec, *factory_spec.sub_agents):
        assert agent.guardrails is not None
        for policy in agent.guardrails.policies:
            module_name, _, function_name = policy.function.path.rpartition(".")
            assert hasattr(importlib.import_module(module_name), function_name), (
                policy.function.path
            )


def test_factory_sandboxed_by_default(factory_spec: AgentSpec) -> None:
    for spec in (factory_spec, *factory_spec.sub_agents):
        assert spec.os_env is not None, spec.name
        assert spec.os_env.type == "caller_process", spec.name
        if spec.name != "fixer":
            assert spec.os_env.sandbox is None, spec.name
    # The fixer gets an explicit sandbox with a writable workspace so a clone
    # made there persists across shell calls.
    fixer = next(a for a in factory_spec.sub_agents if a.name == "fixer")
    assert fixer.os_env is not None
    assert fixer.os_env.sandbox is not None
    assert fixer.os_env.sandbox.write_paths == ["."]
