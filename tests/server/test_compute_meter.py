"""Compute meter: generation lifetimes, idle/deadline stops and trial settlement."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server.compute_meter import ComputeMeter, MeteringLimits, parse_metering_limits
from omnigent.server.compute_meter_store import ComputeMeterStore
from omnigent.server.funded_trial import FundedTrialConfig, FundedTrialPolicy
from omnigent.server.funded_trial_store import MICROS_PER_USD, FundedTrialStore
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.stores.conversation_store import SessionConnectivity
from omnigent.stores.host_store import HostStore

_PROVIDER = "cloud-test"
_HOST = "a1" * 16
_HOUR = 3600
_START = 1_000_000
_ENVIRON = {"OMNIGENT_FUNDED_TRIAL_SIGNING_KEY": "s" * 40}


class _Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> int:
        return self.now


class _Launcher:
    provider = _PROVIDER

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def terminate(self, sandbox_id: str) -> None:
        self.terminated.append(sandbox_id)


class _Sessions:
    """Runner heartbeats for the sessions bound to one host."""

    def __init__(self) -> None:
        self.runner_last_seen: int | None = None

    def list_host_conversation_ids(
        self, host_id: str, *, after: str | None = None, limit: int = 32
    ) -> list[str]:
        return [] if after is not None else [f"{host_id}-session"]

    def get_session_connectivity(self, ids: list[str]) -> dict[str, SessionConnectivity]:
        return {
            session_id: SessionConnectivity(
                runner_id="runner",
                host_id=None,
                needs_workspace=False,
                runner_last_seen=self.runner_last_seen,
            )
            for session_id in ids
        }


def _meter(
    db_uri: str,
    *,
    funding: str,
    limits: MeteringLimits | None,
    output_publisher: Any = None,
) -> dict[str, Any]:
    clock = _Clock()
    launcher = _Launcher()
    trial_store = FundedTrialStore(db_uri, clock=clock)
    policy = FundedTrialPolicy(
        enabled=True,
        store=trial_store,
        config=FundedTrialConfig.from_mapping({"compute_usd_per_hour": {_PROVIDER: 0.5}}),
        environ=_ENVIRON,
        clock=clock,
    )
    deployment = ManagedSandboxDeployment(
        configs=(
            ManagedSandboxConfig(
                server_url="https://s.example.com",
                launcher_factory=lambda: launcher,
                token_ttl_s=3600,
                provider=_PROVIDER,
                funding=funding,
                metering=limits,
            ),
        ),
        funded_trial=policy,
    )
    host_store = HostStore(db_uri)
    sessions = _Sessions()
    meter = ComputeMeter(
        store=ComputeMeterStore(db_uri, clock=clock),
        host_store=host_store,
        conversation_store=sessions,
        sandbox_config=deployment,
        clock=clock,
        output_publisher=output_publisher,
    )
    host_store.register_managed_host(
        host_id=_HOST,
        name="managed-a1a1",
        user_id="owner",
        token="launch-token",
        provider=_PROVIDER,
        sandbox_id="sb-1",
        token_expires_at=_START + _HOUR,
    )
    return {
        "clock": clock,
        "launcher": launcher,
        "meter": meter,
        "sessions": sessions,
        "host_store": host_store,
        "policy": policy,
        "trial_store": trial_store,
    }


def _reserve(env: dict[str, Any], root: str, launch: str = "1") -> Any:
    return env["policy"].reserve_compute(
        "owner",
        provider=_PROVIDER,
        idempotency_key=f"compute:{root}:{launch}",
        root_session_id=root,
        session_id=root,
        hours=4,
    )


def _grant(env: dict[str, Any]) -> None:
    env["trial_store"].upsert_grant(
        "owner",
        allowance_micros=100 * MICROS_PER_USD,
        expires_at=_START + 30 * 24 * _HOUR,
        capabilities=["compute"],
        max_concurrent_roots=1,
        actor_id="operator",
    )


def test_idle_trial_generation_stops_and_settles_measured_runtime(db_uri: str) -> None:
    env = _meter(
        db_uri,
        funding="trial",
        limits=MeteringLimits(idle_shutdown_s=900, max_lifetime_s=4 * _HOUR),
    )
    _grant(env)
    charge = _reserve(env, "root-1")
    usage = env["meter"].record_start(owner="owner", host_id=_HOST, trial_charge_id=charge.id)
    assert usage.deadline_at == _START + 4 * _HOUR

    # A runner seen 10 minutes in keeps the generation alive past the first idle window.
    env["clock"].now = _START + 600
    env["sessions"].runner_last_seen = _START + 600
    assert env["meter"].sweep_once() == 0
    env["sessions"].runner_last_seen = None
    env["clock"].now = _START + 600 + 899
    assert env["meter"].sweep_once() == 0
    env["clock"].now = _START + 600 + 900
    assert env["meter"].sweep_once() == 1

    assert env["launcher"].terminated == ["sb-1"]
    host = env["host_store"].get_host(_HOST)
    assert host.sandbox_id is None  # the next message relaunches a fresh generation
    ended = env["meter"].store.get(usage.id)
    assert (ended.end_reason, ended.seconds) == ("idle", 1500)
    grant = env["trial_store"].get_grant("owner")
    # 1500 s at $0.50/h, rounded up to whole micro-USD; the 4 h hold is released.
    assert (grant.reserved_micros, grant.spent_micros) == (0, 208_334)
    # The freed concurrency slot admits another root.
    assert _reserve(env, "root-2").status == "reserved"
    assert env["meter"].sweep_once() == 0  # settled exactly once


def test_deadline_stops_a_busy_generation(db_uri: str) -> None:
    env = _meter(
        db_uri, funding="trial", limits=MeteringLimits(idle_shutdown_s=900, max_lifetime_s=_HOUR)
    )
    usage = env["meter"].record_start(owner="owner", host_id=_HOST)
    env["clock"].now = _START + _HOUR
    env["sessions"].runner_last_seen = env["clock"].now
    assert env["meter"].sweep_once() == 1
    assert env["meter"].store.get(usage.id).end_reason == "deadline"
    assert env["launcher"].terminated == ["sb-1"]


def test_replaced_generation_is_closed_without_terminating_the_new_one(db_uri: str) -> None:
    env = _meter(
        db_uri, funding="trial", limits=MeteringLimits(idle_shutdown_s=900, max_lifetime_s=_HOUR)
    )
    usage = env["meter"].record_start(owner="owner", host_id=_HOST)
    env["host_store"].replace_managed_host_sandbox(
        host_id=_HOST,
        user_id="owner",
        token="next-token",
        provider=_PROVIDER,
        sandbox_id="sb-2",
        token_expires_at=_START + _HOUR,
    )
    env["clock"].now = _START + 60
    assert env["meter"].sweep_once() == 1
    assert env["meter"].store.get(usage.id).end_reason == "replaced"
    assert env["launcher"].terminated == []
    assert env["host_store"].get_host(_HOST).sandbox_id == "sb-2"


def test_operator_funded_generation_is_recorded_but_never_stopped(db_uri: str) -> None:
    env = _meter(db_uri, funding="deployment", limits=MeteringLimits())
    usage = env["meter"].record_start(owner="owner", host_id=_HOST)
    assert usage.deadline_at is None and usage.funding == "deployment"
    env["clock"].now = _START + 30 * _HOUR
    assert env["meter"].sweep_once() == 0
    assert env["launcher"].terminated == []
    assert env["meter"].store.user_seconds_since("owner", _START) == 30 * _HOUR


def test_metering_limits_parse_and_paid_targets_cannot_disable_them() -> None:
    assert parse_metering_limits(None, funding="deployment", default_lifetime_s=None) == (
        MeteringLimits()
    )
    assert parse_metering_limits(None, funding="trial", default_lifetime_s=4 * _HOUR) == (
        MeteringLimits(idle_shutdown_s=900, max_lifetime_s=4 * _HOUR)
    )
    with pytest.raises(ValueError, match="required"):
        parse_metering_limits(
            {"max_lifetime_s": None}, funding="trial", default_lifetime_s=4 * _HOUR
        )
    with pytest.raises(ValueError, match="positive"):
        parse_metering_limits(
            {"idle_shutdown_s": 0}, funding="deployment", default_lifetime_s=None
        )
    with pytest.raises(ValueError, match="mapping"):
        parse_metering_limits({"idle": 5}, funding="deployment", default_lifetime_s=None)


def test_launch_holds_the_meter_will_not_settle_are_released(db_uri: str) -> None:
    from omnigent.server.routes._sessions.orchestration import _record_managed_generation

    env = _meter(
        db_uri, funding="trial", limits=MeteringLimits(idle_shutdown_s=900, max_lifetime_s=_HOUR)
    )
    _grant(env)
    deployment = env["meter"]._sandbox_config
    deployment.compute_meter = env["meter"]
    first = _reserve(env, "root-1")
    _record_managed_generation(deployment, owner="owner", host_id=_HOST, charge=first)
    # A second launch observing the same open generation must not hold allowance twice.
    second = _reserve(env, "root-1", "2")
    _record_managed_generation(deployment, owner="owner", host_id=_HOST, charge=second)
    store = env["trial_store"]
    assert store.get_charge(first.id).status == "reserved"
    assert store.get_charge(second.id).status == "released"
    # Without a meter nothing could settle the hold, so it is released immediately.
    deployment.compute_meter = None
    third = _reserve(env, "root-1", "3")
    _record_managed_generation(deployment, owner="owner", host_id=_HOST, charge=third)
    assert store.get_charge(third.id).status == "released"


def test_outputs_checkpoint_on_new_activity_and_publish_before_stop(db_uri: str) -> None:
    published: list[tuple[str, str]] = []

    def publish(launcher: Any, host: Any, reason: str) -> bool:
        # The sandbox must still be attached (and not terminated) when outputs are read.
        assert host.sandbox_id == "sb-1" and launcher.terminated == []
        published.append((host.host_id, reason))
        return True

    env = _meter(
        db_uri,
        funding="trial",
        limits=MeteringLimits(idle_shutdown_s=900, max_lifetime_s=4 * _HOUR),
        output_publisher=publish,
    )
    env["meter"].record_start(owner="owner", host_id=_HOST)
    env["clock"].now = _START + 60
    env["sessions"].runner_last_seen = _START + 60
    env["meter"].sweep_once()
    # Activity again inside the checkpoint interval is batched into a later checkpoint.
    env["clock"].now = _START + 120
    env["sessions"].runner_last_seen = _START + 120
    env["meter"].sweep_once()
    assert published == [(_HOST, "checkpoint")]
    env["clock"].now = _START + 200
    env["meter"].sweep_once()
    assert published == [(_HOST, "checkpoint")] * 2
    env["clock"].now = _START + 120 + 900
    assert env["meter"].sweep_once() == 1
    assert published[-1] == (_HOST, "idle")
    assert env["launcher"].terminated == ["sb-1"]
