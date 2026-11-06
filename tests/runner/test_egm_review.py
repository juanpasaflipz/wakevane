"""Review protocol regressions use synthetic artifacts and a real local Git index."""

import json
import subprocess

import pytest
import yaml

from omnigent.runner.egm_review import ReviewLedger


@pytest.fixture
def review(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = repo / "source.ts"
    source.write_text("export const total = 0;\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    output = repo / "docs/designs/egm/run"
    output.mkdir(parents=True)
    spec = output / "r1.md"
    contract = output / "requirements-v1.md"
    spec.write_text(
        "---\ndecisions: []\nacceptance_corrections: []\n---\nDesign with acceptance checks.\n"
    )
    contract.write_text(
        '---\noriginal_task: "One test per rule."\nrequirements:\n'
        '  - id: REQ-1\n    source_quote: "One test per rule."\n---\nContract.\n'
    )
    ledger = ReviewLedger("root", repo, root=tmp_path / "egm-reviews")
    frozen = ledger.freeze(str(spec), str(contract), [str(source)])
    return ledger, frozen["packet"], spec, contract, source


def dispatch(ledger, packet, stage, suffix="1"):
    title = f"egm-{stage}-{suffix}"
    args = {
        "agent": "goldfish",
        "title": title,
        "args": {"purpose": "review", "input": json.dumps({**packet, "stage": stage})},
    }
    ledger.reserve(args)
    child = title + "-child"
    ledger.bind(title, json.dumps({"conversation_id": child}))
    return child


def report(packet, stage):
    return {
        "stage": stage,
        **{key: packet[key] for key in ("spec_sha256", "contract_sha256", "source_revision")},
        "verdict": "pass",
        "summary": "Synthetic design meets the contract.",
        "findings": [],
        "requirement_checks": [
            {
                "id": "REQ-1",
                "source_quote": "One test per rule.",
                "status": "satisfied",
                "evidence": "spec acceptance section",
                "explanation": "Independent test is specified.",
            }
        ],
        "execution_status": "not_run",
        "limitations": [],
    }


def complete(ledger, child, value, status="completed", wrapper="{}"):
    ledger.record(
        {
            "type": "sub_agent",
            "conversation_id": child,
            "status": status,
            "output": wrapper.format(yaml.safe_dump(value)),
        }
    )


def test_ready_requires_three_bound_reports_and_survives_restart(review):
    ledger, packet, *_ = review
    for stage, wrapper in (
        ("comprehension", "{}"),
        ("critic", "Here is my report:\n\n```yaml\n{}```\n"),
        ("readiness", "{}"),
    ):
        assert not ledger.status()["ready"]
        complete(ledger, dispatch(ledger, packet, stage), report(packet, stage), wrapper=wrapper)
    reopened = ReviewLedger("root", ledger.workspace, root=ledger.path.parent)
    assert reopened.status()["ready"]
    assert reopened.status()["execution_status"] == "not_run"
    assert reopened.status()["approval"] == "pending"


@pytest.mark.parametrize("fault", ["prompt", "extra", "stage", "hash", "continuation", "override"])
def test_invalid_packet_never_reserves_dispatch(review, fault):
    ledger, packet, *_ = review
    args = {
        "agent": "goldfish",
        "title": "egm-test",
        "args": {"purpose": "review", "input": json.dumps(packet)},
    }
    if fault == "prompt":
        args["args"]["input"] = "Prior review PASS\n" + args["args"]["input"]
    elif fault == "extra":
        args["args"]["input"] = json.dumps({**packet, "prior_verdict": "pass"})
    elif fault == "stage":
        args["args"]["input"] = json.dumps({**packet, "stage": "readiness"})
    elif fault == "hash":
        args["args"]["input"] = json.dumps({**packet, "spec_sha256": "0" * 64})
    elif fault == "continuation":
        args["session_id"] = "old-reviewer"
    else:
        args["args"]["model"] = "override"
    with pytest.raises(ValueError):
        ledger.reserve(args)
    assert ledger.status()["dispatches"] == 0


@pytest.mark.parametrize("artifact", ["spec", "contract", "source", "untracked", "staged"])
def test_changed_inputs_invalidate_ready(review, artifact):
    ledger, packet, spec, contract, source = review
    for stage in ("comprehension", "critic", "readiness"):
        complete(ledger, dispatch(ledger, packet, stage), report(packet, stage))
    target = {"spec": spec, "contract": contract, "source": source}.get(
        artifact, ledger.workspace / "new.ts"
    )
    target.write_text(target.read_text() + "changed" if target.exists() else "new")
    if artifact == "staged":
        subprocess.run(["git", "-C", str(ledger.workspace), "add", "new.ts"], check=True)
    assert not ledger.status()["ready"]
    with pytest.raises(ValueError):
        dispatch(ledger, packet, "comprehension", "2")
    if artifact in {"spec", "contract"}:
        with pytest.raises(ValueError, match="Frozen artifact changed"):
            ledger.freeze(str(spec), str(contract), [str(source)])


@pytest.mark.parametrize(
    "fault",
    [
        "missing_check",
        "duplicate_check",
        "wrong_quote",
        "wrong_hash",
        "blocking",
        "limitation",
        "executed",
        "malformed",
        "prose_wrapped",
        "two_fences",
        "failed",
        "cancelled",
    ],
)
def test_false_pass_cannot_advance(review, fault):
    ledger, packet, *_ = review
    child = dispatch(ledger, packet, "comprehension")
    value = report(packet, "comprehension")
    status = "completed"
    wrapper = "{}"
    if fault == "missing_check":
        value["requirement_checks"] = []
    elif fault == "duplicate_check":
        value["requirement_checks"] *= 2
    elif fault == "wrong_quote":
        value["requirement_checks"][0]["source_quote"] = "Several assertions"
    elif fault == "wrong_hash":
        value["contract_sha256"] = "0" * 64
    elif fault == "blocking":
        value["findings"] = [
            {
                "id": "f1",
                "severity": "blocking",
                "kind": "editorial",
                "evidence": "acceptance section",
                "problem": "Unresolved correction",
                "requested_change": "Revise spec",
            }
        ]
    elif fault == "limitation":
        value["limitations"] = ["Source unavailable"]
    elif fault == "executed":
        value["execution_status"] = "passed"
    elif fault == "malformed":
        value = "PASS"
    elif fault == "prose_wrapped":
        wrapper = "I reviewed the packet.\n\n---\n\n{}"
    elif fault == "two_fences":
        wrapper = "```yaml\n{0}```\nRevised:\n```yaml\n{0}```"
    else:
        status = fault
    complete(ledger, child, value, status, wrapper=wrapper)
    assert not ledger.status()["ready"]
    with pytest.raises(
        ValueError, match="Prior stages have not passed this revision: comprehension"
    ):
        dispatch(ledger, packet, "critic")


@pytest.mark.parametrize("field", ["decisions", "acceptance_corrections"])
def test_material_open_items_block_readiness_before_paid_dispatch(review, field):
    ledger, _, spec, contract, source = review
    metadata = {"decisions": [], "acceptance_corrections": []}
    metadata[field] = [
        {"id": "open-1", "status": "open", "material": True, "evidence": "No accepted decision"}
    ]
    revision = spec.with_name("r2.md")
    revision.write_text("---\n" + yaml.safe_dump(metadata) + "---\nRevised design.\n")
    packet = ledger.freeze(str(revision), str(contract), [str(source)])["packet"]
    for stage in ("comprehension", "critic"):
        complete(ledger, dispatch(ledger, packet, stage), report(packet, stage))
    with pytest.raises(ValueError, match="Unresolved"):
        dispatch(ledger, packet, "readiness")
    assert ledger.status()["dispatches"] == 2


def test_forged_results_duplicate_titles_and_budget(review):
    ledger, packet, *_ = review
    complete(ledger, "unbound-child", report(packet, "comprehension"))
    assert "comprehension" in ledger.status()["missing_stages"]
    child = dispatch(ledger, packet, "comprehension")
    with pytest.raises(ValueError, match="already used"):
        dispatch(ledger, packet, "comprehension")
    complete(ledger, child, report(packet, "comprehension"), "failed")
    for i in range(2, 10):
        child = dispatch(ledger, packet, "comprehension", str(i))
        complete(ledger, child, report(packet, "comprehension"), "failed")
    with pytest.raises(ValueError, match="Nine-dispatch"):
        dispatch(ledger, packet, "comprehension", "10")


def test_new_revision_resets_passes_and_revision_limit(review):
    ledger, packet, spec, contract, source = review
    complete(ledger, dispatch(ledger, packet, "comprehension"), report(packet, "comprehension"))
    for number in (2, 3):
        revised = spec.with_name(f"r{number}.md")
        revised.write_text(spec.read_text() + f"Revision {number}")
        packet = ledger.freeze(str(revised), str(contract), [str(source)])["packet"]
        assert "comprehension" in ledger.status()["missing_stages"]
    revised = spec.with_name("r4.md")
    revised.write_text(spec.read_text() + "Fourth revision")
    with pytest.raises(ValueError, match="Three-revision"):
        ledger.freeze(str(revised), str(contract), [str(source)])


def test_concurrent_dispatches_cannot_both_launch(review):
    from concurrent.futures import ThreadPoolExecutor

    ledger, packet, *_ = review

    def reserve(number):
        try:
            return dispatch(ledger, packet, "comprehension", str(number))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert ledger.status()["dispatches"] == 1


@pytest.mark.parametrize("proxied", [False, True])
def test_dispatch_and_inbox_wiring(review, monkeypatch, proxied):
    import asyncio
    from pathlib import Path

    import httpx

    from omnigent.runner import tool_dispatch
    from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
    from omnigent.spec import load
    from omnigent.tools.manager import ToolManager
    from tests.runner.test_mcp_routes import Harness

    ledger, _packet, spec_path, contract, source = review
    spec = load(Path(__file__).resolve().parents[2] / "examples/egm")
    assert "sys_egm_review" in ToolManager(spec, os_env_schema_only=True).get_tool_names()
    monkeypatch.setattr(
        "omnigent.process_logging.data_dir", lambda: ledger.path.parent.parent / "runtime"
    )
    calls = []

    async def launch(args, **kwargs):
        calls.append(args)
        return json.dumps({"conversation_id": args["title"] + "-child"})

    monkeypatch.setattr(tool_dispatch, "_execute_subagent_tool", launch)

    async def policy(payload, **kwargs):
        return tool_dispatch._SubagentInboxEvaluation(payload)

    async def cleanup(*args, **kwargs):
        pass

    monkeypatch.setattr(tool_dispatch, "_evaluate_subagent_inbox_output", policy)
    monkeypatch.setattr(tool_dispatch, "_cleanup_drained_subagent_work", cleanup)
    harness = Harness(runner_workspace=ledger.workspace)
    harness.spec_cache["integration"] = spec
    deny_next = False

    async def forward(request):
        nonlocal deny_next
        assert request.url.path == "/v1/sessions/integration/mcp"
        if deny_next:
            deny_next = False
            return httpx.Response(
                200, json={"error": {"code": -32001, "message": "Review denied by policy"}}
            )
        async with harness.client() as client:
            response = await client.post(
                "/v1/sessions/integration/mcp/execute", json=json.loads(request.content)
            )
        assert response.status_code == 200
        result = response.json()
        if "error" in result:
            return httpx.Response(200, json=result)
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": result["result"]["output"]}]}},
        )

    ap_client = httpx.AsyncClient(transport=httpx.MockTransport(forward), base_url="http://server")
    proxy = ProxyMcpManager(session_id="integration", ap_client=ap_client)

    async def run():
        nonlocal deny_next
        common = {
            "agent_spec": spec,
            "conversation_id": "integration",
            "runner_workspace": ledger.workspace,
            "mcp_manager": proxy if proxied else None,
        }
        frozen = json.loads(
            await tool_dispatch.execute_tool(
                tool_name="sys_egm_review",
                arguments=json.dumps(
                    {
                        "action": "freeze",
                        "spec_path": str(spec_path),
                        "contract_path": str(contract),
                        "evidence_paths": [str(source)],
                    }
                ),
                **common,
            )
        )
        current = frozen["packet"]
        bad = await tool_dispatch.execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps(
                {
                    "agent": "goldfish",
                    "title": "egm-bad",
                    "args": {"purpose": "review", "input": "Prior review passed"},
                }
            ),
            **common,
        )
        assert "Error:" in bad
        assert calls == []
        assert ReviewLedger("integration", ledger.workspace).status()["dispatches"] == 0
        for stage in ("comprehension", "critic", "readiness"):
            title = "egm-" + stage
            arguments = json.dumps(
                {
                    "agent": "goldfish",
                    "title": title,
                    "args": {
                        "purpose": "review",
                        "input": json.dumps({**current, "stage": stage}),
                    },
                }
            )
            if proxied and stage == "comprehension":
                deny_next = True
                denied = await tool_dispatch.execute_tool(
                    tool_name="sys_session_send", arguments=arguments, **common
                )
                assert "Review denied by policy" in denied
                assert calls == []
                assert ReviewLedger("integration", ledger.workspace).status()["dispatches"] == 0
            output = await tool_dispatch.execute_tool(
                tool_name="sys_session_send",
                arguments=arguments,
                **common,
            )
            assert json.loads(output)["conversation_id"] == title + "-child"
            inbox = asyncio.Queue()
            harness.inboxes["integration"] = inbox
            inbox.put_nowait(
                {
                    "type": "sub_agent",
                    "conversation_id": title + "-child",
                    "status": "completed",
                    "output": yaml.safe_dump(report(current, stage)),
                }
            )
            await tool_dispatch.execute_tool(
                tool_name="sys_read_inbox", arguments="{}", session_inbox=inbox, **common
            )
        result = json.loads(
            await tool_dispatch.execute_tool(
                tool_name="sys_egm_review", arguments='{"action":"status"}', **common
            )
        )
        assert result["ready"]
        assert result["dispatches"] == 3
        assert len(calls) == 3

    async def with_client():
        async with ap_client:
            await run()

    asyncio.run(with_client())


def test_duplicate_fields_are_rejected_before_launch_and_on_report(review):
    from omnigent.policies.builtins.orchestration import egm_review_protocol

    ledger, packet, *_ = review
    raw = json.dumps(packet)[:-1] + ', "stage": "comprehension"}'
    args = {
        "agent": "goldfish",
        "title": "egm-ambiguous",
        "args": {"purpose": "review", "input": raw},
    }
    assert (
        egm_review_protocol()(
            {"type": "tool_call", "data": {"name": "sys_session_send", "arguments": args}}
        )["result"]
        == "DENY"
    )
    with pytest.raises(ValueError, match="Duplicate"):
        ledger.reserve(args)
    child = dispatch(ledger, packet, "comprehension")
    output = yaml.safe_dump(report(packet, "comprehension")) + "verdict: pass\n"
    ledger.record(
        {"type": "sub_agent", "conversation_id": child, "status": "completed", "output": output}
    )
    assert not ledger.status()["reviews"][0]["passed"]
    assert "Duplicate" in ledger.status()["reviews"][0]["error"]


def test_inbox_persistence_failure_preserves_evidence(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from omnigent.runner import tool_dispatch

    payload = {
        "type": "sub_agent",
        "conversation_id": "child",
        "status": "completed",
        "output": "report",
    }
    monkeypatch.setattr(
        tool_dispatch,
        "_evaluate_subagent_inbox_output",
        AsyncMock(return_value=tool_dispatch._SubagentInboxEvaluation(payload)),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(tool_dispatch, "_cleanup_drained_subagent_work", cleanup)

    class UnavailableLedger:
        def record(self, payload):
            raise OSError("Temporary storage outage")

    async def run():
        inbox = asyncio.Queue()
        inbox.put_nowait(payload)
        result = await tool_dispatch._drain_inbox(inbox, review_ledger=UnavailableLedger())
        assert "could not be persisted" in result
        assert inbox.get_nowait() == payload
        cleanup.assert_not_called()

    asyncio.run(run())


def test_early_completion_is_retained_until_child_binding(review):
    ledger, packet, *_ = review
    title = "egm-fast-review"
    ledger.reserve(
        {
            "agent": "goldfish",
            "title": title,
            "args": {"purpose": "review", "input": json.dumps(packet)},
        }
    )
    payload = {
        "type": "sub_agent",
        "title": title,
        "conversation_id": "fast-child",
        "status": "completed",
        "output": yaml.safe_dump(report(packet, "comprehension")),
    }
    with pytest.raises(ValueError, match="not bound yet"):
        ledger.record(payload)
    ledger.bind(title, json.dumps({"conversation_id": "fast-child"}))
    ledger.record(payload)
    assert "comprehension" not in ledger.status()["missing_stages"]


def test_new_file_design_freezes_existing_readme_without_creating_implementation(review):
    ledger, _packet, spec, contract, _source = review
    readme = ledger.workspace / "README"
    readme.write_text("A repository with no greeting implementation yet.\n")
    frozen = ledger.freeze(str(spec), str(contract), [str(readme)])
    assert str(readme) in frozen["packet"]["evidence"]
    assert not (ledger.workspace / "shared/greeting.py").exists()
    assert not (ledger.workspace / "tests/test_greeting.py").exists()


def test_completion_rechecks_ready_artifacts_and_fails_closed(review, monkeypatch):
    from omnigent.runner.egm_review import ReviewOutputGate

    ledger, packet, spec, *_ = review
    monkeypatch.setattr("omnigent.process_logging.data_dir", lambda: ledger.path.parent.parent)
    gate = ReviewOutputGate("root", ledger.workspace)
    assert "incomplete" in gate.completion()
    child = dispatch(ledger, packet, "comprehension")
    assert "Waiting for comprehension" in gate.completion()
    complete(ledger, child, report(packet, "comprehension"))
    for stage in ("critic", "readiness"):
        complete(ledger, dispatch(ledger, packet, stage), report(packet, stage))
    assert "ready for implementation" in gate.completion()
    spec.write_text(spec.read_text() + "Changed after review.\n")
    assert "Runtime ready=false" in gate.completion()
    ledger.path.write_bytes(b"corrupt database")
    assert "Readiness has not been established" in gate.completion()


@pytest.mark.asyncio
@pytest.mark.parametrize("coordinator", [True, False])
async def test_runner_stream_replaces_only_coordinator_prose(review, monkeypatch, coordinator):
    from pathlib import Path

    from omnigent.runner import create_runner_app
    from omnigent.spec import load
    from omnigent.spec.types import AgentSpec
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _sse,
    )
    from tests.runner.helpers import NullServerClient

    ledger, *_ = review
    monkeypatch.setattr("omnigent.process_logging.data_dir", lambda: ledger.path.parent.parent)
    spec = (
        load(Path(__file__).resolve().parents[2] / "examples/egm")
        if coordinator
        else AgentSpec(spec_version=1, name="ordinary-agent")
    )
    fabricated = "UNSUPPORTED_READY: implement immediately"
    message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": fabricated}],
    }
    frames = [
        {"type": "response.created", "response": {"id": "resp_test"}},
        {"type": "response.output_text.delta", "delta": fabricated},
        {"type": "response.output_text.done", "text": fabricated},
        {"type": "response.output_item.done", "item": message},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call_output",
                "call_id": "prior_tool",
                "output": "tool evidence unchanged",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_test",
                "output": [message],
                "output_text": fabricated,
                "usage": {"total_tokens": 42},
            },
        },
    ]
    harness = _ScriptedHarnessClient([_sse(frame) for frame in frames])

    async def resolver(_agent_id, _session_id=None):
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),
        spec_resolver=resolver,
        server_client=NullServerClient(),
        runner_workspace=ledger.workspace,
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions/root/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "test-agent",
                "harness": "claude-sdk",
                "content": [{"type": "input_text", "text": "review"}],
            },
        )
    assert response.status_code == 200, response.text
    assert "tool evidence unchanged" in response.text
    events = [
        json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    terminal = next(event for event in events if event["type"] == "response.completed")
    assert terminal["response"]["usage"] == {"total_tokens": 42}
    if coordinator:
        from omnigent.runner.app import _session_histories_ref

        assert fabricated not in response.text
        assert "Runtime ready=false" in response.text
        histories = _session_histories_ref["root"]
        assert fabricated not in json.dumps(histories)
    else:
        assert fabricated in response.text


def test_packet_explains_the_baseline_and_lists_dirty_files(review):
    import hashlib

    from omnigent.runner.egm_review import BASELINE_METHOD, ReviewLedger

    ledger, packet, spec, contract, source = review
    # A clean checkout's baseline is the digest of canonical "{}", as the method says.
    assert packet["baseline_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert packet["baseline_dirty_files"] == {}
    assert packet["baseline_method"] == BASELINE_METHOD
    assert packet["baseline_sha256"] in BASELINE_METHOD
    # Dirty inputs are inventoried by path and content hash, and change the digest.
    dirty = source.parent / "notes.txt"
    dirty.write_bytes(b"scratch\n")
    revised = spec.with_name("r2.md")
    revised.write_text(spec.read_text() + "\nRevised.\n")
    fresh = ReviewLedger("root", ledger.workspace, root=ledger.path.parent)
    later = fresh.freeze(str(revised), str(contract), [str(source)])["packet"]
    assert later["baseline_dirty_files"] == {str(dirty): hashlib.sha256(b"scratch\n").hexdigest()}
    assert later["baseline_sha256"] != packet["baseline_sha256"]
    # Design outputs never count as dirt, so the first revision stays verifiable.
    dirty.unlink()
    assert fresh.status()["revision"] == 2


def test_report_fence_may_follow_prose_on_the_same_line():
    from omnigent.runner.egm_review import _report_text

    body = "stage: comprehension\nverdict: pass\n"
    assert _report_text(f"Done checking.```yaml\n{body}```\n") == body
    assert _report_text(f"Report:\n\n```yaml\n{body}```") == body
    with pytest.raises(ValueError, match="exactly one"):
        _report_text(f"```yaml\n{body}```\nand again ```yaml\n{body}```\n")


@pytest.mark.parametrize(
    "history",
    [
        "Reviewer egm-run-r1-comprehension-2 flagged this.",
        "Resolved finding comprehension-1 by rewording.",
        "A fresh-context reviewer noted the gap.",
        "Chosen per the review protocol's guidance on boundaries.",
        "Split the cases, following the instruction to check allocation boundaries.",
    ],
)
def test_freeze_refuses_artifacts_that_carry_review_history(review, history):
    from omnigent.runner.egm_review import ReviewLedger

    ledger, _, spec, contract, source = review
    revised = spec.with_name("r2.md")
    revised.write_text(spec.read_text() + f"\n{history}\n")
    fresh = ReviewLedger("root", ledger.workspace, root=ledger.path.parent)
    with pytest.raises(ValueError, match="mentions review history"):
        fresh.freeze(str(revised), str(contract), [str(source)])
    # Nothing was frozen, so no revision or dispatch budget was spent.
    assert fresh.status()["revision"] == 1
    # Design rationale without review references still freezes.
    revised.write_text(spec.read_text() + "\nRationale: the earlier draft was ambiguous.\n")
    assert fresh.freeze(str(revised), str(contract), [str(source)])["revision"] == 2


@pytest.mark.parametrize("kind", ["acceptance", "behavior"])
def test_nonblocking_findings_advance_and_stay_on_record(review, kind):
    ledger, packet, *_ = review
    child = dispatch(ledger, packet, "comprehension")
    value = report(packet, "comprehension")
    value["findings"] = [
        {
            "id": "f1",
            "severity": "nonblocking",
            "kind": kind,
            "evidence": "acceptance section",
            "problem": "Worth tightening before implementation",
            "requested_change": "Reword the expected outcome",
        }
    ]
    complete(ledger, child, value, "completed")
    status = ledger.status()
    passed = [r for r in status["reviews"] if r["stage"] == "comprehension"]
    assert passed and passed[-1]["passed"] is True
    # The next stage opens on the same revision; the advice travels with the report.
    dispatch(ledger, packet, "critic")
    assert status["revision"] == 1
