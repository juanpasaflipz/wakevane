"""Persistence for managed sandbox generation lifetimes.

Each provisioned generation gets one row. Closing a row is a single-use
``UPDATE … WHERE ended_at IS NULL``, so concurrent sweeps on several replicas
settle each generation exactly once.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlManagedComputeUsage, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

FUNDING_MODES: frozenset[str] = frozenset({"deployment", "trial", "billing"})


@dataclass(frozen=True)
class ComputeUsage:
    """One sandbox generation's metered lifetime."""

    id: str
    workspace_id: int
    user_id: str
    host_id: str
    provider: str
    sandbox_id: str
    funding: str
    started_at: int
    last_active_at: int
    deadline_at: int | None
    ended_at: int | None
    end_reason: str | None
    trial_charge_id: str | None
    billed_at: int | None

    @property
    def seconds(self) -> int:
        """Billable seconds; only meaningful once the generation has ended."""
        return max(0, (self.ended_at or self.started_at) - self.started_at)


def _to_usage(row: SqlManagedComputeUsage) -> ComputeUsage:
    return ComputeUsage(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        host_id=row.host_id,
        provider=row.provider,
        sandbox_id=row.sandbox_id,
        funding=row.funding,
        started_at=row.started_at,
        last_active_at=row.last_active_at,
        deadline_at=row.deadline_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        trial_charge_id=row.trial_charge_id,
        billed_at=row.billed_at,
    )


class ComputeMeterStore:
    """SQLAlchemy-backed generation lifetimes.

    :param storage_location: SQLAlchemy database URI shared with the other
        server stores.
    """

    def __init__(self, storage_location: str, *, clock: Any = time.time) -> None:
        self.storage_location = storage_location
        self._clock = clock
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.compute_meter_store"
        )
        # Serializes the open-record check with its insert on SQLite.
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.compute_meter_store", immediate=True
        )

    def _now(self) -> int:
        return int(self._clock())

    def start(
        self,
        *,
        user_id: str,
        host_id: str,
        provider: str,
        sandbox_id: str,
        funding: str,
        deadline_at: int | None,
        trial_charge_id: str | None = None,
    ) -> ComputeUsage:
        """Open the generation's record; a replay returns the open record."""
        if funding not in FUNDING_MODES:
            raise ValueError(f"Unknown compute funding {funding!r}")
        now = self._now()

        def write(session: Session) -> ComputeUsage:
            existing = session.scalars(
                select(SqlManagedComputeUsage).where(
                    SqlManagedComputeUsage.workspace_id == current_workspace_id(),
                    SqlManagedComputeUsage.provider == provider,
                    SqlManagedComputeUsage.sandbox_id == sandbox_id,
                    SqlManagedComputeUsage.ended_at.is_(None),
                )
            ).first()
            if existing is not None:
                return _to_usage(existing)
            row = SqlManagedComputeUsage(
                workspace_id=current_workspace_id(),
                id=uuid.uuid4().hex,
                user_id=user_id,
                host_id=host_id,
                provider=provider,
                sandbox_id=sandbox_id,
                funding=funding,
                started_at=now,
                last_active_at=now,
                deadline_at=deadline_at,
                trial_charge_id=trial_charge_id,
            )
            session.add(row)
            return _to_usage(row)

        return run_write_transaction(
            self._session_immediate, "insert_managed_compute_usage", write
        )

    def list_open(self, *, limit: int = 500) -> list[ComputeUsage]:
        """Open generations across every workspace, oldest first."""
        with self._session("select_open_managed_compute_usage") as session:
            rows = session.scalars(
                select(SqlManagedComputeUsage)
                .where(SqlManagedComputeUsage.ended_at.is_(None))
                .order_by(SqlManagedComputeUsage.started_at, SqlManagedComputeUsage.id)
                .limit(limit)
            )
            return [_to_usage(row) for row in rows]

    def get(self, usage_id: str) -> ComputeUsage | None:
        """Return one record in the current workspace."""
        with self._session("select_managed_compute_usage") as session:
            row = session.get(SqlManagedComputeUsage, (current_workspace_id(), usage_id))
            return _to_usage(row) if row is not None else None

    def touch(self, usage_id: str, last_active_at: int) -> None:
        """Advance the generation's latest observed activity (never backwards)."""
        with self._session("update_managed_compute_usage_activity") as session:
            session.execute(
                update(SqlManagedComputeUsage)
                .where(
                    SqlManagedComputeUsage.workspace_id == current_workspace_id(),
                    SqlManagedComputeUsage.id == usage_id,
                    SqlManagedComputeUsage.ended_at.is_(None),
                    SqlManagedComputeUsage.last_active_at < last_active_at,
                )
                .values(last_active_at=last_active_at)
                .execution_options(synchronize_session=False)
            )

    def end(self, usage_id: str, *, reason: str, ended_at: int | None = None) -> bool:
        """Close the record once; ``False`` when another sweep already did."""
        when = ended_at if ended_at is not None else self._now()
        with self._session("update_managed_compute_usage_end") as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlManagedComputeUsage)
                    .where(
                        SqlManagedComputeUsage.workspace_id == current_workspace_id(),
                        SqlManagedComputeUsage.id == usage_id,
                        SqlManagedComputeUsage.ended_at.is_(None),
                    )
                    .values(ended_at=when, end_reason=reason)
                    .execution_options(synchronize_session=False)
                ),
            )
            return result.rowcount == 1

    def list_unbilled(self, *, funding: str, limit: int = 500) -> list[ComputeUsage]:
        """Ended, not yet reported generations for one payer, oldest first."""
        with self._session("select_unbilled_managed_compute_usage") as session:
            rows = session.scalars(
                select(SqlManagedComputeUsage)
                .where(
                    SqlManagedComputeUsage.funding == funding,
                    SqlManagedComputeUsage.ended_at.is_not(None),
                    SqlManagedComputeUsage.billed_at.is_(None),
                )
                .order_by(SqlManagedComputeUsage.ended_at, SqlManagedComputeUsage.id)
                .limit(limit)
            )
            return [_to_usage(row) for row in rows]

    def mark_billed(self, usage_id: str) -> bool:
        """Record that usage was reported; ``False`` when already reported."""
        with self._session("update_managed_compute_usage_billed") as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlManagedComputeUsage)
                    .where(
                        SqlManagedComputeUsage.workspace_id == current_workspace_id(),
                        SqlManagedComputeUsage.id == usage_id,
                        SqlManagedComputeUsage.billed_at.is_(None),
                    )
                    .values(billed_at=self._now())
                    .execution_options(synchronize_session=False)
                ),
            )
            return result.rowcount == 1

    def user_seconds_since(self, user_id: str, since: int) -> int:
        """Seconds the account's generations ran since *since*, open ones to now."""
        now = self._now()
        with self._session("select_managed_compute_usage_for_user") as session:
            rows = session.scalars(
                select(SqlManagedComputeUsage).where(
                    SqlManagedComputeUsage.workspace_id == current_workspace_id(),
                    SqlManagedComputeUsage.user_id == user_id,
                    (SqlManagedComputeUsage.ended_at.is_(None))
                    | (SqlManagedComputeUsage.ended_at > since),
                )
            )
            return sum(max(0, (row.ended_at or now) - max(row.started_at, since)) for row in rows)
