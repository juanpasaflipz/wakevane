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
GrantKind = Literal["trial", "period_credit", "overage"]
CAPABILITIES: frozenset[str] = frozenset({"inference", "compute", "coding", "pull_request"})
GRANT_KINDS: frozenset[str] = frozenset({"trial", "period_credit", "overage"})
_CHARGE_KINDS: frozenset[str] = frozenset({"inference", "compute", "storage"})


def apply_markup(amount_micros: int, markup_percent: int) -> int:
    """Scale a list-price amount by a grant's markup, rounding up."""
    if markup_percent <= 0:
        return amount_micros
    scaled = amount_micros * (100 + markup_percent)
    return -(-scaled // 100)


@dataclass(frozen=True)
class TrialGrant:
    """A funding grant as read from the store.

    ``kind`` says who pays: ``trial`` is an operator grant, ``period_credit``
    a subscription period's included usage and ``overage`` the metered spend
    beyond it. Subscription grants carry ``billing_period_end`` and a
    ``markup_percent`` applied to every hold and settlement.
    """

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
    kind: str = "trial"
    billing_period_end: int = 0
    markup_percent: int = 0

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
    reported_at: int | None = None
    report_ref: str | None = None


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
        kind=row.kind,
        billing_period_end=row.billing_period_end,
        markup_percent=row.markup_percent,
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
        reported_at=row.reported_at,
        report_ref=row.report_ref,
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
    def _grant_row(
        session: Session,
        user_id: str,
        kind: str = "trial",
        billing_period_end: int | None = None,
    ) -> SqlFundedTrialGrant | None:
        """The account's grant of one kind; the latest period when several exist."""
        query = select(SqlFundedTrialGrant).where(
            SqlFundedTrialGrant.workspace_id == current_workspace_id(),
            SqlFundedTrialGrant.user_id == user_id,
            SqlFundedTrialGrant.kind == kind,
        )
        if billing_period_end is not None:
            query = query.where(SqlFundedTrialGrant.billing_period_end == billing_period_end)
        return session.scalars(
            query.order_by(
                SqlFundedTrialGrant.billing_period_end.desc(),
                SqlFundedTrialGrant.created_at.desc(),
            ).limit(1)
        ).one_or_none()

    @staticmethod
    def _grant_row_by_id(session: Session, grant_id: str) -> SqlFundedTrialGrant | None:
        return session.get(SqlFundedTrialGrant, (current_workspace_id(), grant_id))

    def get_grant(self, user_id: str, kind: str = "trial") -> TrialGrant | None:
        """Return the account's grant of *kind*, active or not."""
        with self._session("select_funded_trial_grant") as session:
            row = self._grant_row(session, user_id, kind)
            return _to_grant(row) if row is not None else None

    def get_grant_by_id(self, grant_id: str) -> TrialGrant | None:
        """Return one grant in the current workspace."""
        with self._session("select_funded_trial_grant_by_id") as session:
            row = self._grant_row_by_id(session, grant_id)
            return _to_grant(row) if row is not None else None

    def list_grants(self, user_id: str) -> list[TrialGrant]:
        """Every grant the account holds, newest period first."""
        with self._session("list_funded_trial_grants") as session:
            rows = session.scalars(
                select(SqlFundedTrialGrant)
                .where(
                    SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                    SqlFundedTrialGrant.user_id == user_id,
                )
                .order_by(
                    SqlFundedTrialGrant.billing_period_end.desc(),
                    SqlFundedTrialGrant.created_at.desc(),
                )
            )
            return [_to_grant(row) for row in rows]

    def upsert_grant(
        self,
        user_id: str,
        *,
        allowance_micros: int,
        expires_at: int,
        capabilities: Iterable[str],
        max_concurrent_roots: int,
        actor_id: str | None,
        kind: str = "trial",
        billing_period_end: int = 0,
        markup_percent: int = 0,
    ) -> TrialGrant:
        """Create or replace the account's grant terms, keeping recorded spend.

        Grants are keyed by account, kind and billing period, so a new
        billing period creates a fresh ledger while replacing terms within a
        period keeps its reserved and spent amounts. Replacing terms
        reactivates a revoked grant.
        """
        requested = frozenset(capabilities)
        unknown = requested - CAPABILITIES
        if unknown:
            raise ValueError(f"Unknown trial capabilities: {sorted(unknown)}")
        if kind not in GRANT_KINDS:
            raise ValueError(f"Unknown grant kind {kind!r}")
        if kind == "trial" and billing_period_end != 0:
            raise ValueError("Trial grants have no billing period")
        if allowance_micros < 0 or max_concurrent_roots < 1 or markup_percent < 0:
            raise ValueError(
                "Allowance and markup must be non-negative and concurrency at least 1"
            )
        now = self._now()
        if expires_at <= now:
            raise ValueError("A trial grant must expire in the future")

        def write(session: Session) -> TrialGrant:
            row = self._grant_row(session, user_id, kind, billing_period_end)
            event = "grant_updated"
            if row is None:
                event = "granted"
                row = SqlFundedTrialGrant(
                    id=f"ftg_{uuid.uuid4().hex}",
                    user_id=user_id,
                    kind=kind,
                    billing_period_end=billing_period_end,
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
            row.markup_percent = markup_percent
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
                        "kind": kind,
                        "billing_period_end": billing_period_end,
                        "allowance_micros": allowance_micros,
                        "expires_at": expires_at,
                        "capabilities": sorted(requested),
                        "max_concurrent_roots": max_concurrent_roots,
                        "markup_percent": markup_percent,
                    },
                    now=now,
                )
            )
            return _to_grant(row)

        return run_write_transaction(self._session_immediate, "upsert_funded_trial_grant", write)

    def revoke_grant(
        self, user_id: str, *, actor_id: str | None, kind: str = "trial"
    ) -> TrialGrant | None:
        """Block new spending from the account's latest grant of *kind*.

        Open reservations stay settleable.
        """
        now = self._now()

        def write(session: Session) -> TrialGrant | None:
            row = self._grant_row(session, user_id, kind)
            if row is None:
                return None
            self._revoke_row(session, row, actor_id=actor_id, now=now)
            return _to_grant(row)

        return run_write_transaction(self._session_immediate, "revoke_funded_trial_grant", write)

    def revoke_grants(
        self, user_id: str, *, kinds: Iterable[str], actor_id: str | None
    ) -> list[TrialGrant]:
        """Revoke every unrevoked grant of the given kinds, e.g. on cancellation."""
        now = self._now()
        wanted = frozenset(kinds)

        def write(session: Session) -> list[TrialGrant]:
            rows = session.scalars(
                select(SqlFundedTrialGrant).where(
                    SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                    SqlFundedTrialGrant.user_id == user_id,
                    SqlFundedTrialGrant.kind.in_(sorted(wanted)),
                    SqlFundedTrialGrant.revoked_at.is_(None),
                )
            ).all()
            for row in rows:
                self._revoke_row(session, row, actor_id=actor_id, now=now)
            return [_to_grant(row) for row in rows]

        return run_write_transaction(self._session_immediate, "revoke_funded_trial_grants", write)

    @staticmethod
    def _revoke_row(
        session: Session, row: SqlFundedTrialGrant, *, actor_id: str | None, now: int
    ) -> None:
        if row.revoked_at is not None:
            return
        row.revoked_at = now
        row.generation += 1
        session.add(
            _event_row(
                event="revoked",
                grant_id=row.id,
                user_id=row.user_id,
                actor_id=actor_id,
                detail={"kind": row.kind},
                now=now,
            )
        )

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
        grant_id: str | None = None,
    ) -> TrialCharge:
        """Atomically hold ``amount_micros`` of the account's allowance.

        ``amount_micros`` is the list-price cost; the grant's markup is added
        before the hold. Without ``grant_id`` the account's trial grant pays.
        Replaying ``idempotency_key`` returns the existing charge without a
        second hold. Denials are audited and raise :class:`FundedTrialDenied`.
        """
        if kind not in _CHARGE_KINDS:
            raise ValueError(f"Unknown charge kind {kind!r}")
        if amount_micros <= 0:
            raise ValueError("A reservation must be positive")
        now = self._now()
        list_micros = amount_micros

        def write(session: Session) -> TrialCharge:
            row = (
                self._grant_row_by_id(session, grant_id)
                if grant_id is not None
                else self._grant_row(session, user_id)
            )
            if row is None or row.user_id != user_id:
                raise FundedTrialDenied("no_grant", "This account has no funded trial.")
            grant = _to_grant(row)
            amount_micros = apply_markup(list_micros, grant.markup_percent)
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
                # Concurrency is per account: a root funded by period credit and
                # another by overage still count against the same limit.
                account_grants = select(SqlFundedTrialGrant.id).where(
                    SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                    SqlFundedTrialGrant.user_id == user_id,
                )
                open_roots = set(
                    session.scalars(
                        select(SqlFundedTrialCharge.root_session_id)
                        .where(
                            SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                            SqlFundedTrialCharge.grant_id.in_(account_grants),
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
            recorded_detail = dict(detail or {})
            if grant.markup_percent:
                recorded_detail["list_micros"] = list_micros
                recorded_detail["markup_percent"] = grant.markup_percent
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
                detail=json.dumps(recorded_detail, sort_keys=True) if recorded_detail else None,
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
                    "amount_micros": list_micros,
                    "grant_id": grant_id,
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
            grant_terms = self._grant_row_by_id(session, row.grant_id)
            charged_micros = apply_markup(
                settled_micros, grant_terms.markup_percent if grant_terms is not None else 0
            )
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlFundedTrialCharge)
                    .where(
                        SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                        SqlFundedTrialCharge.id == charge_id,
                        SqlFundedTrialCharge.status == "reserved",
                    )
                    .values(status=status, settled_micros=charged_micros, settled_at=now)
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
                        spent_micros=SqlFundedTrialGrant.spent_micros + charged_micros,
                    )
                    .execution_options(synchronize_session=False)
                )
                if charged_micros > row.reserved_micros:
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
                                "settled_micros": charged_micros,
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
        """Record measured list-price cost once; later calls return the first settlement.

        The grant's markup is added to ``actual_micros`` before it is spent.
        """
        if actual_micros < 0:
            raise ValueError("A settled cost cannot be negative")
        return self._close(
            charge_id, status="settled", settled_micros=actual_micros, detail=detail
        )

    def settled_unreported(self, *, kind: str, limit: int = 500) -> list[tuple[TrialCharge, str]]:
        """Settled, non-zero charges on grants of *kind* not yet reported, with their owner."""
        with self._session("select_unreported_funded_trial_charges") as session:
            rows = session.execute(
                select(SqlFundedTrialCharge, SqlFundedTrialGrant.user_id)
                .join(
                    SqlFundedTrialGrant,
                    and_(
                        SqlFundedTrialGrant.workspace_id == SqlFundedTrialCharge.workspace_id,
                        SqlFundedTrialGrant.id == SqlFundedTrialCharge.grant_id,
                    ),
                )
                .where(
                    SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                    SqlFundedTrialGrant.kind == kind,
                    SqlFundedTrialCharge.status == "settled",
                    SqlFundedTrialCharge.settled_micros > 0,
                    SqlFundedTrialCharge.reported_at.is_(None),
                )
                .order_by(SqlFundedTrialCharge.settled_at, SqlFundedTrialCharge.id)
                .limit(limit)
            )
            return [(_to_charge(charge), user_id) for charge, user_id in rows]

    def mark_reported(self, charge_id: str, *, report_ref: str) -> bool:
        """Record that a charge reached the billing provider; ``False`` if already done."""
        now = self._now()

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlFundedTrialCharge)
                    .where(
                        SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                        SqlFundedTrialCharge.id == charge_id,
                        SqlFundedTrialCharge.reported_at.is_(None),
                    )
                    .values(reported_at=now, report_ref=report_ref)
                    .execution_options(synchronize_session=False)
                ),
            )
            return result.rowcount == 1

        return run_write_transaction(self._session, "mark_funded_trial_charge_reported", write)

    def recent_charges(self, user_id: str, *, limit: int = 20) -> list[TrialCharge]:
        """Latest charges across all of the account's grants, newest first."""
        with self._session("select_recent_funded_trial_charges") as session:
            grant_ids = select(SqlFundedTrialGrant.id).where(
                SqlFundedTrialGrant.workspace_id == current_workspace_id(),
                SqlFundedTrialGrant.user_id == user_id,
            )
            rows = session.scalars(
                select(SqlFundedTrialCharge)
                .where(
                    SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                    SqlFundedTrialCharge.grant_id.in_(grant_ids),
                )
                .order_by(SqlFundedTrialCharge.created_at.desc(), SqlFundedTrialCharge.id)
                .limit(limit)
            )
            return [_to_charge(row) for row in rows]

    def release(self, charge_id: str) -> TrialCharge:
        """Return an unused reservation; only safe when nothing was dispatched."""
        return self._close(charge_id, status="released", settled_micros=0, detail=None)

    def get_charge(self, charge_id: str) -> TrialCharge | None:
        """Return one charge in the current workspace."""
        with self._session("select_funded_trial_charge") as session:
            row = session.get(SqlFundedTrialCharge, (current_workspace_id(), charge_id))
            return _to_charge(row) if row is not None else None

    def find_charge(self, grant_id: str, idempotency_key: str) -> TrialCharge | None:
        """Return the charge a replayed key already created on *grant_id*, if any."""
        with self._session("select_funded_trial_charge_by_key") as session:
            row = session.scalars(
                select(SqlFundedTrialCharge).where(
                    SqlFundedTrialCharge.workspace_id == current_workspace_id(),
                    SqlFundedTrialCharge.grant_id == grant_id,
                    SqlFundedTrialCharge.idempotency_key == idempotency_key,
                )
            ).one_or_none()
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
