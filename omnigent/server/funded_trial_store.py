"""Persistence for service-funded trial grants and their shared spending ledger.

Every billable operation reserves its conservative maximum cost before it
runs. The reservation is one conditional ``UPDATE`` on the grant row, so two
concurrent reservations can never jointly exceed the allowance. Settlement
and release are single-use transitions of the charge row (``UPDATE … WHERE
status = 'reserved'`` + rowcount), which makes them idempotent under retries.

Amounts are integer micro-USD. See ``docs/FUNDED_TRIAL.md`` for the contract.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlFundedTrialCharge,
    SqlFundedTrialEvent,
    SqlFundedTrialGrant,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

MICROS_PER_USD = 1_000_000

Capability = Literal["inference", "compute", "coding", "pull_request"]
ChargeKind = Literal["inference", "compute", "storage"]
CAPABILITIES: frozenset[str] = frozenset({"inference", "compute", "coding", "pull_request"})
_CHARGE_KINDS: frozenset[str] = frozenset({"inference", "compute", "storage"})


@dataclass(frozen=True)
class TrialGrant:
    """A trial grant as read from the store."""

    id: str
    user_id: str
    capabilities: frozenset[str]
    allowance_micros: int
    reserved_micros: int
    spent_micros: int
    max_concurrent_roots: int
    generation: int
    expires_at: int
    revoked_at: int | None
    created_by: str | None
    created_at: int

    @property
    def remaining_micros(self) -> int:
        """Allowance not yet spent or held by an open reservation."""
        return max(0, self.allowance_micros - self.reserved_micros - self.spent_micros)

    def inactive_reason(self, now: int) -> str | None:
        """Why this grant cannot fund new work, or ``None`` when it can."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return None


@dataclass(frozen=True)
class TrialCharge:
    """One reservation and, once settled, its measured cost."""

    id: str
    grant_id: str
    idempotency_key: str
    kind: str
    status: str
    root_session_id: str | None
    session_id: str | None
    reserved_micros: int
    settled_micros: int | None
    created_at: int
    settled_at: int | None


class FundedTrialDenied(Exception):
    """A funded operation was refused; ``reason`` is a stable machine code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _to_grant(row: SqlFundedTrialGrant) -> TrialGrant:
    return TrialGrant(
        id=row.id,
        user_id=row.user_id,
        capabilities=frozenset(filter(None, row.capabilities.split(","))),
        allowance_micros=row.allowance_micros,
        reserved_micros=row.reserved_micros,
        spent_micros=row.spent_micros,
        max_concurrent_roots=row.max_concurrent_roots,
        generation=row.generation,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _to_charge(row: SqlFundedTrialCharge) -> TrialCharge:
    return TrialCharge(
        id=row.id,
        grant_id=row.grant_id,
        idempotency_key=row.idempotency_key,
        kind=row.kind,
        status=row.status,
        root_session_id=row.root_session_id,
        session_id=row.session_id,
        reserved_micros=row.reserved_micros,
        settled_micros=row.settled_micros,
        created_at=row.created_at,
        settled_at=row.settled_at,
    )


def _event_row(
    *,
    event: str,
    grant_id: str | None,
    user_id: str | None,
    actor_id: str | None,
    detail: dict[str, Any] | None,
    now: int,
) -> SqlFundedTrialEvent:
    return SqlFundedTrialEvent(
        id=f"fte_{uuid.uuid4().hex}",
        grant_id=grant_id,
        user_id=user_id,
        actor_id=actor_id,
        event=event,
        detail=json.dumps(detail, sort_keys=True) if detail else None,
        created_at=now,
    )


class FundedTrialStore:
    """SQLAlchemy-backed grants, reservations and audit events.

    :param storage_location: SQLAlchemy database URI shared with the other
        server stores.
    """

    def __init__(self, storage_location: str, *, clock: Any = time.time) -> None:
        self.storage_location = storage_location
        self._clock = clock
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.funded_trial_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.funded_trial_store", immediate=True
        )

    def _now(self) -> int:
        return int(self._clock())

    @staticmethod
    def _grant_row(session: Session, user_id: str) -> SqlFundedTrialGrant | None:
        return session.scalars(
            select(SqlFundedTrialGrant).where(
                SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                SqlFundedTrialGrant.user_id == user_id,
            )
        ).one_or_none()

    def get_grant(self, user_id: str) -> TrialGrant | None:
        """Return the account's grant, active or not."""
        with self._session("select_funded_trial_grant") as session:
            row = self._grant_row(session, user_id)
            return _to_grant(row) if row is not None else None

    def upsert_grant(
        self,
        user_id: str,
        *,
        allowance_micros: int,
        expires_at: int,
        capabilities: Iterable[str],
        max_concurrent_roots: int,
        actor_id: str | None,
    ) -> TrialGrant:
        """Create or replace the account's grant terms, keeping recorded spend.

        Replacing terms reactivates a revoked grant; the existing reserved and
        spent amounts continue to count against the new allowance.
        """
        requested = frozenset(capabilities)
        unknown = requested - CAPABILITIES
        if unknown:
            raise ValueError(f"Unknown trial capabilities: {sorted(unknown)}")
        if allowance_micros < 0 or max_concurrent_roots < 1:
            raise ValueError("Allowance must be non-negative and concurrency at least 1")
        now = self._now()
        if expires_at <= now:
            raise ValueError("A trial grant must expire in the future")

        def write(session: Session) -> TrialGrant:
            row = self._grant_row(session, user_id)
            event = "grant_updated"
            if row is None:
                event = "granted"
                row = SqlFundedTrialGrant(
                    id=f"ftg_{uuid.uuid4().hex}",
                    user_id=user_id,
                    reserved_micros=0,
                    spent_micros=0,
                    generation=0,
                    created_at=now,
                    created_by=actor_id,
                )
                session.add(row)
            row.capabilities = ",".join(sorted(requested))
            row.allowance_micros = allowance_micros
            row.expires_at = expires_at
            row.max_concurrent_roots = max_concurrent_roots
            row.revoked_at = None
            row.generation += 1
            session.flush()
            session.add(
                _event_row(
                    event=event,
                    grant_id=row.id,
                    user_id=user_id,
                    actor_id=actor_id,
                    detail={
                        "allowance_micros": allowance_micros,
                        "expires_at": expires_at,
                        "capabilities": sorted(requested),
                        "max_concurrent_roots": max_concurrent_roots,
                    },
                    now=now,
                )
            )
            return _to_grant(row)

        return run_write_transaction(self._session_immediate, "upsert_funded_trial_grant", write)

    def revoke_grant(self, user_id: str, *, actor_id: str | None) -> TrialGrant | None:
        """Block new spending for the account. Open reservations stay settleable."""
        now = self._now()

        def write(session: Session) -> TrialGrant | None:
            row = self._grant_row(session, user_id)
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = now
                row.generation += 1
                session.add(
                    _event_row(
                        event="revoked",
                        grant_id=row.id,
                        user_id=user_id,
                        actor_id=actor_id,
                        detail=None,
                        now=now,
                    )
                )
            return _to_grant(row)

        return run_write_transaction(self._session_immediate, "revoke_funded_trial_grant", write)

    def reserve(
        self,
        user_id: str,
        *,
        capability: str,
        kind: str,
        amount_micros: int,
        idempotency_key: str,
        root_session_id: str | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TrialCharge:
        """Atomically hold ``amount_micros`` of the account's allowance.

        Replaying ``idempotency_key`` returns the existing charge without a
        second hold. Denials are audited and raise :class:`FundedTrialDenied`.
        """
        if kind not in _CHARGE_KINDS:
            raise ValueError(f"Unknown charge kind {kind!r}")
        if amount_micros <= 0:
            raise ValueError("A reservation must be positive")
        now = self._now()

        def write(session: Session) -> TrialCharge:
            row = self._grant_row(session, user_id)
            if row is None:
                raise FundedTrialDenied("no_grant", "This account has no funded trial.")
            grant = _to_grant(row)
            existing = session.scalars(
                select(SqlFundedTrialCharge).where(
                    SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                    SqlFundedTrialCharge.grant_id == grant.id,
                    SqlFundedTrialCharge.idempotency_key == idempotency_key,
                )
            ).one_or_none()
            if existing is not None:
                return _to_charge(existing)
            inactive = grant.inactive_reason(now)
            if inactive is not None:
                raise FundedTrialDenied(inactive, f"The funded trial is {inactive}.")
            if capability not in grant.capabilities:
                raise FundedTrialDenied(
                    "capability", f"The funded trial does not include {capability}."
                )
            # The conditional UPDATE is the admission point and also locks the
            # grant row, serializing the concurrency count below.
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlFundedTrialGrant)
                    .where(
                        SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                        SqlFundedTrialGrant.id == grant.id,
                        SqlFundedTrialGrant.revoked_at.is_(None),
                        SqlFundedTrialGrant.expires_at > now,
                        SqlFundedTrialGrant.reserved_micros
                        + SqlFundedTrialGrant.spent_micros
                        + amount_micros
                        <= SqlFundedTrialGrant.allowance_micros,
                    )
                    .values(reserved_micros=SqlFundedTrialGrant.reserved_micros + amount_micros)
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount != 1:
                raise FundedTrialDenied(
                    "allowance_exhausted",
                    "The funded trial allowance cannot cover this operation.",
                )
            if root_session_id is not None:
                open_roots = set(
                    session.scalars(
                        select(SqlFundedTrialCharge.root_session_id)
                        .where(
                            SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                            SqlFundedTrialCharge.grant_id == grant.id,
                            SqlFundedTrialCharge.status == "reserved",
                            SqlFundedTrialCharge.root_session_id.is_not(None),
                        )
                        .distinct()
                    )
                )
                if (
                    root_session_id not in open_roots
                    and len(open_roots) >= grant.max_concurrent_roots
                ):
                    raise FundedTrialDenied(
                        "concurrency",
                        "Another funded workflow is already running for this account.",
                    )
            charge = SqlFundedTrialCharge(
                id=f"ftc_{uuid.uuid4().hex}",
                grant_id=grant.id,
                idempotency_key=idempotency_key,
                kind=kind,
                status="reserved",
                root_session_id=root_session_id,
                session_id=session_id,
                reserved_micros=amount_micros,
                settled_micros=None,
                detail=json.dumps(detail, sort_keys=True) if detail else None,
                created_at=now,
                settled_at=None,
            )
            session.add(charge)
            session.flush()
            return _to_charge(charge)

        try:
            return run_write_transaction(self._session_immediate, "reserve_funded_trial", write)
        except FundedTrialDenied as denied:
            self.record_event(
                "denied",
                user_id=user_id,
                actor_id=user_id,
                detail={
                    "reason": denied.reason,
                    "capability": capability,
                    "kind": kind,
                    "amount_micros": amount_micros,
                    "root_session_id": root_session_id,
                },
            )
            raise

    def _close(
        self, charge_id: str, *, status: str, settled_micros: int, detail: dict[str, Any] | None
    ) -> TrialCharge:
        now = self._now()

        def write(session: Session) -> TrialCharge:
            row = session.get(SqlFundedTrialCharge, (current_workspace_id(), charge_id))
            if row is None:
                raise KeyError(charge_id)
            if row.status != "reserved":
                return _to_charge(row)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlFundedTrialCharge)
                    .where(
                        SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                        SqlFundedTrialCharge.id == charge_id,
                        SqlFundedTrialCharge.status == "reserved",
                    )
                    .values(status=status, settled_micros=settled_micros, settled_at=now)
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount == 1:
                session.execute(
                    update(SqlFundedTrialGrant)
                    .where(
                        SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                        SqlFundedTrialGrant.id == row.grant_id,
                    )
                    .values(
                        reserved_micros=SqlFundedTrialGrant.reserved_micros - row.reserved_micros,
                        spent_micros=SqlFundedTrialGrant.spent_micros + settled_micros,
                    )
                    .execution_options(synchronize_session=False)
                )
                if settled_micros > row.reserved_micros:
                    grant_row = session.get(
                        SqlFundedTrialGrant, (current_workspace_id(), row.grant_id)
                    )
                    session.add(
                        _event_row(
                            event="overrun",
                            grant_id=row.grant_id,
                            user_id=grant_row.user_id if grant_row is not None else None,
                            actor_id=None,
                            detail={
                                "charge_id": charge_id,
                                "reserved_micros": row.reserved_micros,
                                "settled_micros": settled_micros,
                                **(detail or {}),
                            },
                            now=now,
                        )
                    )
            session.refresh(row)
            return _to_charge(row)

        return run_write_transaction(self._session_immediate, f"{status}_funded_trial", write)

    def settle(
        self, charge_id: str, actual_micros: int, *, detail: dict[str, Any] | None = None
    ) -> TrialCharge:
        """Record measured cost once; later calls return the first settlement."""
        if actual_micros < 0:
            raise ValueError("A settled cost cannot be negative")
        return self._close(
            charge_id, status="settled", settled_micros=actual_micros, detail=detail
        )

    def release(self, charge_id: str) -> TrialCharge:
        """Return an unused reservation; only safe when nothing was dispatched."""
        return self._close(charge_id, status="released", settled_micros=0, detail=None)

    def get_charge(self, charge_id: str) -> TrialCharge | None:
        """Return one charge in the current workspace."""
        with self._session("select_funded_trial_charge") as session:
            row = session.get(SqlFundedTrialCharge, (current_workspace_id(), charge_id))
            return _to_charge(row) if row is not None else None

    def open_charges(self, user_id: str) -> list[TrialCharge]:
        """Unsettled reservations for the account, oldest first."""
        with self._session("select_open_funded_trial_charges") as session:
            grant = self._grant_row(session, user_id)
            if grant is None:
                return []
            rows = session.scalars(
                select(SqlFundedTrialCharge)
                .where(
                    and_(
                        SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                        SqlFundedTrialCharge.grant_id == grant.id,
                        SqlFundedTrialCharge.status == "reserved",
                    )
                )
                .order_by(SqlFundedTrialCharge.created_at, SqlFundedTrialCharge.id)
            )
            return [_to_charge(row) for row in rows]

    def count_events(self, user_id: str, event: str) -> int:
        """Number of audit events of one type for the account."""
        with self._session("count_funded_trial_events") as session:
            return int(
                session.scalar(
                    select(func.count()).where(
                        SqlFundedTrialEvent.workspace_id == current_workspace_id(),
                        SqlFundedTrialEvent.user_id == user_id,
                        SqlFundedTrialEvent.event == event,
                    )
                )
                or 0
            )

    def list_events(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Most recent audit events for the account."""
        with self._session("list_funded_trial_events") as session:
            rows = session.scalars(
                select(SqlFundedTrialEvent)
                .where(
                    SqlFundedTrialEvent.workspace_id == current_workspace_id(),
                    SqlFundedTrialEvent.user_id == user_id,
                )
                .order_by(SqlFundedTrialEvent.created_at.desc(), SqlFundedTrialEvent.id)
                .limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "event": row.event,
                    "actor_id": row.actor_id,
                    "detail": json.loads(row.detail) if row.detail else None,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def record_event(
        self,
        event: str,
        *,
        user_id: str | None,
        actor_id: str | None,
        detail: dict[str, Any] | None = None,
        grant_id: str | None = None,
    ) -> None:
        """Append an audit event in its own transaction."""
        now = self._now()

        def write(session: Session) -> None:
            session.add(
                _event_row(
                    event=event,
                    grant_id=grant_id,
                    user_id=user_id,
                    actor_id=actor_id,
                    detail=detail,
                    now=now,
                )
            )

        run_write_transaction(self._session, "insert_funded_trial_event", write)
