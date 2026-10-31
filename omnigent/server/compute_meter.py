"""Meter managed sandbox lifetimes and stop generations nobody is using.

A provisioned generation keeps running (and billing) until something stops
it: Modal holds a sandbox open for its 24-hour cap even after the in-sandbox
runner exits on idle. The meter records every generation's lifetime and, per
sandbox target, enforces an idle shutdown and a hard lifetime. Ended
generations settle their funded trial compute hold from measured runtime and
remain available to the billing reporter.

Stopping a generation detaches it from its durable host exactly like the
stale-generation reaper, so the session's next message relaunches a fresh
sandbox; provider termination failures stay pending for the reaper to retry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.server.compute_meter_store import ComputeMeterStore, ComputeUsage

_logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 60.0
DEFAULT_IDLE_SHUTDOWN_S = 15 * 60
_HOST_SESSION_PAGE = 100
# Minimum spacing between checkpoints of one generation's outputs.
CHECKPOINT_INTERVAL_S = 120

OutputPublisher = Callable[[Any, Any, str], bool]
"""``(launcher, host, reason) -> succeeded``: copies a live sandbox's durable
outputs out before they can be lost (see :mod:`omnigent.server.egm_outputs`)."""


@dataclass(frozen=True)
class MeteringLimits:
    """Per-target enforcement; ``None`` disables that rule.

    :param idle_shutdown_s: Stop a generation this long after the last runner
        activity observed on its host.
    :param max_lifetime_s: Stop a generation this long after it started.
    """

    idle_shutdown_s: int | None = None
    max_lifetime_s: int | None = None


def parse_metering_limits(
    raw: object, *, funding: str, default_lifetime_s: int | None
) -> MeteringLimits:
    """Parse ``sandbox.metering``; paid targets get enforcement by default.

    Operator-funded targets keep their historical unbounded lifetime unless
    configured. Trial and billed targets default to an idle shutdown and the
    lifetime their reservation covers, and may not disable either rule.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {"idle_shutdown_s", "max_lifetime_s"}:
        raise ValueError(
            "server config 'sandbox.metering' must be a mapping of idle_shutdown_s/max_lifetime_s"
        )
    paid = funding != "deployment"

    def seconds(name: str, default: int | None) -> int | None:
        value = raw.get(name, default)
        if value is None:
            if paid:
                raise ValueError(
                    f"server config 'sandbox.metering.{name}' is required for {funding!r} targets"
                )
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"server config 'sandbox.metering.{name}' must be positive whole seconds"
            )
        return value

    return MeteringLimits(
        idle_shutdown_s=seconds("idle_shutdown_s", DEFAULT_IDLE_SHUTDOWN_S if paid else None),
        max_lifetime_s=seconds("max_lifetime_s", default_lifetime_s if paid else None),
    )


class ComputeMeter:
    """Records generation lifetimes and runs the enforcement sweep.

    :param store: Generation lifetime records.
    :param host_store: Durable managed host rows (current sandbox id, heartbeat).
    :param conversation_store: Session bindings and runner heartbeats.
    :param sandbox_config: The deployment, for targets, launchers and the trial policy.
    :param output_publisher: Checkpoints outputs while a generation is active and
        once more before the meter stops it, or ``None``.
    """

    def __init__(
        self,
        *,
        store: ComputeMeterStore,
        host_store: Any,
        conversation_store: Any,
        sandbox_config: Any,
        clock: Callable[[], int] = now_epoch,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        output_publisher: OutputPublisher | None = None,
    ) -> None:
        self.store = store
        self._host_store = host_store
        self._conversation_store = conversation_store
        self._sandbox_config = sandbox_config
        self._clock = clock
        self._sweep_interval_s = sweep_interval_s
        self._task: asyncio.Task[None] | None = None
        self._output_publisher = output_publisher
        # Generation id -> (activity covered, time) of its last successful checkpoint.
        self._checkpoints: dict[str, tuple[int, int]] = {}

    def _target(self, provider: str | None) -> Any:
        return self._sandbox_config.recorded(provider)

    def record_start(
        self,
        *,
        owner: str,
        host_id: str,
        trial_charge_id: str | None = None,
    ) -> ComputeUsage | None:
        """Open the record for the host's current generation after it comes online."""
        host = self._host_store.get_host(host_id)
        if host is None or host.sandbox_id is None or host.sandbox_provider is None:
            return None
        target = self._target(host.sandbox_provider)
        limits: MeteringLimits = getattr(target, "metering", None) or MeteringLimits()
        now = self._clock()
        return self.store.start(
            user_id=owner,
            host_id=host_id,
            provider=host.sandbox_provider,
            sandbox_id=host.sandbox_id,
            funding=getattr(target, "funding", "deployment"),
            deadline_at=now + limits.max_lifetime_s if limits.max_lifetime_s else None,
            trial_charge_id=trial_charge_id,
        )

    def _latest_runner_activity(self, host_id: str) -> int | None:
        latest: int | None = None
        after: str | None = None
        while True:
            ids = self._conversation_store.list_host_conversation_ids(
                host_id, after=after, limit=_HOST_SESSION_PAGE
            )
            if not ids:
                return latest
            for connectivity in self._conversation_store.get_session_connectivity(ids).values():
                seen = connectivity.runner_last_seen
                if seen is not None and (latest is None or seen > latest):
                    latest = seen
            if len(ids) < _HOST_SESSION_PAGE:
                return latest
            after = ids[-1]

    def _finish(self, usage: ComputeUsage, reason: str, ended_at: int) -> bool:
        if not self.store.end(usage.id, reason=reason, ended_at=ended_at):
            return False
        if usage.trial_charge_id is not None:
            policy = getattr(self._sandbox_config, "funded_trial", None)
            if policy is None or policy.store is None:
                _logger.error(
                    "Trial compute charge %s ended without a trial policy; hold stays reserved",
                    usage.trial_charge_id,
                )
            else:
                policy.settle_compute(
                    usage.trial_charge_id,
                    provider=usage.provider,
                    seconds=max(0, ended_at - usage.started_at),
                )
        _logger.info(
            "Managed sandbox %s (%s) metered %ss, ended: %s",
            usage.sandbox_id,
            usage.provider,
            max(0, ended_at - usage.started_at),
            reason,
        )
        return True

    def _stop_reason(self, usage: ComputeUsage, last_active: int, now: int) -> str | None:
        if usage.deadline_at is not None and now >= usage.deadline_at:
            return "deadline"
        limits = getattr(self._target(usage.provider), "metering", None)
        idle = limits.idle_shutdown_s if limits is not None else None
        if idle is not None and now - last_active >= idle:
            return "idle"
        return None

    def _publish_outputs(self, usage: ComputeUsage, host: Any, reason: str, activity: int) -> None:
        from omnigent.server.managed_hosts import _launcher_for_teardown

        if self._output_publisher is None:
            return
        launcher = _launcher_for_teardown(host, self._sandbox_config)
        if launcher is not None and self._output_publisher(launcher, host, reason):
            self._checkpoints[usage.id] = (activity, self._clock())

    def _maybe_checkpoint(self, usage: ComputeUsage, host: Any, activity: int, now: int) -> None:
        covered, at = self._checkpoints.get(usage.id, (-1, 0))
        if activity > covered and now - at >= CHECKPOINT_INTERVAL_S:
            self._publish_outputs(usage, host, "checkpoint", activity)

    def _stop_generation(self, usage: ComputeUsage, host: Any) -> bool:
        from omnigent.server.managed_hosts import _launcher_for_teardown

        if not self._host_store.detach_stale_managed_sandbox(
            usage.host_id, sandbox_id=usage.sandbox_id, expected_updated_at=host.updated_at
        ):
            return False  # A heartbeat or relaunch raced the sweep; retried next sweep.
        launcher = _launcher_for_teardown(host, self._sandbox_config)
        if launcher is None:
            return True  # Detached; the reaper retries termination from the pending slot.
        try:
            launcher.terminate(usage.sandbox_id)
        except Exception:
            _logger.exception(
                "Could not terminate sandbox %s; the reaper will retry", usage.sandbox_id
            )
            return True
        self._host_store.mark_sandbox_terminated(usage.host_id, sandbox_id=usage.sandbox_id)
        return True

    def _sweep_record(self, usage: ComputeUsage, now: int) -> str | None:
        host = self._host_store.get_host(usage.host_id)
        if host is None or host.deleted_at is not None:
            return "gone" if self._finish(usage, "gone", now) else None
        if host.sandbox_id != usage.sandbox_id:
            # Someone else (relaunch, reaper, teardown) already detached it.
            return "replaced" if self._finish(usage, "replaced", now) else None
        activity = self._latest_runner_activity(usage.host_id)
        last_active = max(usage.last_active_at, activity or 0)
        if activity is not None and activity > usage.last_active_at:
            self.store.touch(usage.id, activity)
        reason = self._stop_reason(usage, last_active, now)
        if reason is None:
            self._maybe_checkpoint(usage, host, last_active, now)
            return None
        # Spending limits win over outputs: a failed final export still stops the
        # sandbox, leaving the latest successful checkpoint.
        self._publish_outputs(usage, host, reason, last_active)
        if not self._stop_generation(usage, host):
            return None
        self._checkpoints.pop(usage.id, None)
        return reason if self._finish(usage, reason, now) else None

    def sweep_once(self, *, now: int | None = None) -> int:
        """Run one enforcement pass; returns how many generations ended."""
        reference = self._clock() if now is None else now
        ended = 0
        for usage in self.store.list_open():
            with workspace_scope(usage.workspace_id):
                try:
                    if self._sweep_record(usage, reference) is not None:
                        ended += 1
                except Exception:
                    _logger.exception(
                        "Compute meter failed for sandbox %s; continuing", usage.sandbox_id
                    )
        return ended

    async def start(self) -> None:
        """Start this replica's sweep loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="compute-meter")

    async def shutdown(self) -> None:
        """Stop the sweep loop."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sweep_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Compute meter sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_s)
