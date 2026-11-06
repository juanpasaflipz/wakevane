"""Background loop that reports settled overage charges to the billing provider."""

from __future__ import annotations

import asyncio
import logging

from omnigent.db.db_models import workspace_scope
from omnigent.server.billing import BillingPolicy

_logger = logging.getLogger(__name__)

REPORT_INTERVAL_S = 60.0


async def report_overage_once(policy: BillingPolicy) -> int:
    """Report pending overage charges once, returning how many were sent."""

    def run() -> int:
        with workspace_scope(0):
            return policy.report_overage()

    return await asyncio.to_thread(run)


async def report_overage_loop(
    policy: BillingPolicy, *, interval_s: float = REPORT_INTERVAL_S
) -> None:
    """Report overage every *interval_s* seconds until cancelled.

    A provider outage is logged and retried next round; charges stay
    unreported until Stripe accepts them, so nothing is lost or duplicated.
    """
    while True:
        try:
            sent = await report_overage_once(policy)
            if sent:
                _logger.info("Reported %d overage charge(s) to the billing provider", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Overage reporting failed; will retry")
        await asyncio.sleep(interval_s)
