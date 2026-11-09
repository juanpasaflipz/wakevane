"""Synthetic repository evidence and priced catalogs exercise the planning rules."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from omnigent.server.model_plans import (
    ModelPlanRequest,
    assess_task,
    github_repository,
    inspect_repository,
    recommend_models,
)


def request(**changes):
    return ModelPlanRequest.model_validate(
        {
            "repository": "https://github.com/example/demo.git",
            "prompt": "Fix a typo",
            "configuration_revision": "r1",
            **changes,
        }
    )


def repository(**changes):
    return {
        "name": "example/demo",
        "commit": "a" * 40,
        "files_sampled": 20,
        "partial": False,
        "languages": ["Python"],
        "test_files_detected": True,
        **changes,
    }


def catalog():
    return {
        "configuration_revision": "r1",
        "default_model": "main",
        "models": [
            {"id": "main", "pricing": {"input": 3, "output": 15}},
            {"id": "cheap", "pricing": {"input": 0.2, "output": 0.4}},
            {"id": "unknown"},
        ],
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/demo",
        "https://github.com/example/demo.git/",
        "git@github.com:example/demo.git",
    ],
)
def test_repository_urls(url):
    assert github_repository(url) == "example/demo"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/example/demo",
        "https://github.com@evil.test/example/demo",
        "https://secret@github.com/example/demo",
        "https://github.com/example/../private",
        "https://github.com/example/demo?token=secret",
        "file:///etc/passwd",
        "https://github.com/example/demo/tree/main",
        "git@github.com:example/demo#main",
    ],
)
def test_rejects_arbitrary_hosts_credentials_and_paths(url):
    with pytest.raises(ValueError):
        github_repository(url)


@pytest.mark.parametrize("budget", [0, -1, 1001, float("nan"), float("inf")])
def test_invalid_budgets(budget):
    with pytest.raises(ValidationError):
        request(budget_usd=budget)


def test_simple_task_uses_one_model_and_exact_configured_prices():
    plan = recommend_models(request(mode="economy"), repository(), catalog(), markup_percent=20)
    assert [step["model_id"] for step in plan["steps"]] == ["cheap"]
    assert plan["estimate"]["high_usd"] == pytest.approx(0.0096)
    assert plan["estimate"]["low_usd"] == pytest.approx(0.0024)
    assert plan["budget_enforced"] is False
    assert {row["id"] for row in plan["candidates"]} == {"cheap", "main"}


@pytest.mark.parametrize("mode", ["balanced", "quality"])
def test_preserves_deployment_default_without_assuming_quality_from_price(mode):
    options = catalog()
    options["models"].append({"id": "expensive", "pricing": {"input": 100, "output": 100}})
    plan = recommend_models(request(mode=mode), repository(), options)
    assert plan["steps"][0]["model_id"] == "main"
    assert len(plan["steps"]) == (2 if mode == "quality" else 1)


def test_sensitive_task_adds_manual_review_and_warns_over_budget():
    plan = recommend_models(
        request(prompt="Change payment permissions", budget_usd=0.01),
        repository(),
        catalog(),
        assessment={"risky": False, "broad": False},
    )
    assert plan["complexity"] == "high"
    assert plan["steps"][1]["role"] == "review"
    assert plan["steps"][1]["automatic"] is False
    assert plan["within_budget"] is False
    assert plan["estimate"]["high_usd"] == pytest.approx(0.64092)


def test_partial_repo_and_missing_tests_are_disclosed():
    plan = recommend_models(
        request(), repository(partial=True, test_files_detected=False), catalog()
    )
    assert plan["complexity"] == "high"
    assert any("partial sample" in warning for warning in plan["warnings"])
    assert any("No test files" in warning for warning in plan["warnings"])


@pytest.mark.parametrize("rate", [None, -1, float("nan"), float("inf"), True, "1"])
def test_unpriced_or_malformed_rates_never_recommended(rate):
    options = catalog()
    options["models"] = [{"id": "bad", "pricing": {"input": rate, "output": 1}}]
    with pytest.raises(ValueError, match="No available models"):
        recommend_models(request(), repository(), options)


@pytest.mark.asyncio
async def test_inspection_pins_tree_and_bounds_sample_without_reading_source():
    client = AsyncMock()
    client.repository_request.side_effect = [
        {"sha": "a" * 40},
        {"tree": [{"type": "blob", "path": "tests/test_demo.py"}] * 3001},
    ]
    result = await inspect_repository(
        client, "synthetic-token", request().repository, "feature/test"
    )
    assert result["files_sampled"] == 3000
    assert result["partial"] is True
    assert result["languages"] == ["Python"]
    assert result["test_files_detected"] is True
    calls = client.repository_request.call_args_list
    assert calls[0].args[-1] == "/commits/feature%2Ftest"
    assert calls[1].args[-1] == "/git/trees/" + "a" * 40
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ai_analysis_is_one_batch_with_only_prompt_and_aggregate_evidence():
    calls = []

    def respond(req):
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "scope": {"choice": "high"},
                    "review": {"choice": "required"},
                }
            },
        )

    result = await assess_task(
        "Synthetic task", repository(), "fake-key", transport=httpx.MockTransport(respond)
    )
    assert result == {"broad": True, "risky": True}
    assert len(calls) == 1
    assert set(calls[0]["questions"]) == {"scope", "review"}
    assert calls[0]["state"] == {
        "task": "Synthetic task",
        "repository": {
            "files_sampled": 20,
            "partial": False,
            "languages": ["Python"],
            "test_files_detected": True,
        },
    }


@pytest.mark.asyncio
async def test_invalid_ai_choices_fail_validation():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "answers": {"scope": {"choice": "invented"}, "review": {"choice": "ordinary"}},
            },
        )
    )
    with pytest.raises(ValueError, match="Invalid task assessment"):
        await assess_task("Synthetic", repository(), "fake", transport=transport)
