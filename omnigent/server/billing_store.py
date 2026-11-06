"""Persistence for seat subscriptions mirrored from the billing provider.

Money lives with the provider; this store keeps the per-account subscription
state the server needs to admit spending, plus the provider's webhook event
ids so a redelivered event is processed at most once. Usage itself is held
and settled in :mod:`omnigent.server.funded_trial_store`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBillingAccount, SqlBillingEvent, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

SEAT_STATUSES: frozenset[str] = frozenset({"none", "active", "past_due", "canceled"})


@dataclass(frozen=True)
class BillingAccount:
    """One account's subscription state as read from the store."""

    user_id: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    plan: str
    seat_status: str
    seats: int
    period_start: int | None
    period_end: int | None
    overage_cap_micros: int
    overage_enabled: bool
    created_at: int
    updated_at: int

    def in_period(self, now: int) -> bool:
        """Whether a paid period covers *now*."""
        return self.period_end is not None and now < self.period_end


def _to_account(row: SqlBillingAccount) -> BillingAccount:
    return BillingAccount(
        user_id=row.user_id,
        stripe_customer_id=row.stripe_customer_id,
        stripe_subscription_id=row.stripe_subscription_id,
        plan=row.plan,
        seat_status=row.seat_status,
        seats=row.seats,
        period_start=row.period_start,
        period_end=row.period_end,
        overage_cap_micros=row.overage_cap_micros,
        overage_enabled=row.overage_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class BillingStore:
    """SQLAlchemy-backed billing accounts and webhook event records.

    :param storage_location: SQLAlchemy database URI shared with the other
        server stores.
    """

    def __init__(self, storage_location: str, *, clock: Any = time.time) -> None:
        self.storage_location = storage_location
        self._clock = clock
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.billing_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.billing_store", immediate=True
        )

    def _now(self) -> int:
        return int(self._clock())

    @staticmethod
    def _account_row(session: Session, user_id: str) -> SqlBillingAccount | None:
        return session.get(SqlBillingAccount, (current_workspace_id(), user_id))

    def get_account(self, user_id: str) -> BillingAccount | None:
        """Return the account's subscription state, if it ever subscribed."""
        with self._session("select_billing_account") as session:
            row = self._account_row(session, user_id)
            return _to_account(row) if row is not None else None

    def find_by_customer(self, stripe_customer_id: str) -> BillingAccount | None:
        """Return the account owning a provider customer id."""
        with self._session("select_billing_account_by_customer") as session:
            row = session.scalars(
                select(SqlBillingAccount).where(
                    SqlBillingAccount.workspace_id == current_workspace_id(),
                    SqlBillingAccount.stripe_customer_id == stripe_customer_id,
                )
            ).first()
            return _to_account(row) if row is not None else None

    def find_by_subscription(self, stripe_subscription_id: str) -> BillingAccount | None:
        """Return the account owning a provider subscription id."""
        with self._session("select_billing_account_by_subscription") as session:
            row = session.scalars(
                select(SqlBillingAccount).where(
                    SqlBillingAccount.workspace_id == current_workspace_id(),
                    SqlBillingAccount.stripe_subscription_id == stripe_subscription_id,
                )
            ).first()
            return _to_account(row) if row is not None else None

    def upsert_account(self, user_id: str, **fields: Any) -> BillingAccount:
        """Create or update the account's subscription fields.

        Unknown field names are rejected so a provider payload cannot write
        arbitrary columns. ``plan`` is required on first creation.
        """
        allowed = {
            "stripe_customer_id",
            "stripe_subscription_id",
            "plan",
            "seat_status",
            "seats",
            "period_start",
            "period_end",
            "overage_cap_micros",
            "overage_enabled",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown billing account fields: {sorted(unknown)}")
        status = fields.get("seat_status")
        if status is not None and status not in SEAT_STATUSES:
            raise ValueError(f"Unknown seat status {status!r}")
        if fields.get("overage_cap_micros", 0) < 0:
            raise ValueError("The overage cap cannot be negative")
        now = self._now()

        def write(session: Session) -> BillingAccount:
            row = self._account_row(session, user_id)
            if row is None:
                if not fields.get("plan"):
                    raise ValueError("A new billing account needs a plan")
                row = SqlBillingAccount(
                    user_id=user_id,
                    plan=str(fields["plan"]),
                    seat_status="none",
                    seats=1,
                    overage_cap_micros=0,
                    overage_enabled=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            for name, value in fields.items():
                setattr(row, name, value)
            row.updated_at = now
            session.flush()
            return _to_account(row)

        return run_write_transaction(self._session_immediate, "upsert_billing_account", write)

    def record_event(self, event_id: str, *, event_type: str) -> bool:
        """Insert a provider event once; ``False`` when it was seen before."""
        now = self._now()

        def write(session: Session) -> bool:
            if session.get(SqlBillingEvent, (current_workspace_id(), event_id)) is not None:
                return False
            session.add(
                SqlBillingEvent(id=event_id, type=event_type, received_at=now, processed_at=None)
            )
            return True

        return run_write_transaction(self._session_immediate, "insert_billing_event", write)

    def finish_event(self, event_id: str, *, error: str | None) -> None:
        """Mark an event processed, or record why processing failed."""
        now = self._now()

        def write(session: Session) -> None:
            row = session.get(SqlBillingEvent, (current_workspace_id(), event_id))
            if row is None:
                return
            row.processed_at = None if error else now
            row.error = error

        run_write_transaction(self._session, "update_billing_event", write)

    def unprocessed_event(self, event_id: str) -> bool:
        """Whether a recorded event still awaits successful processing."""
        with self._session("select_billing_event") as session:
            row = session.get(SqlBillingEvent, (current_workspace_id(), event_id))
            return row is not None and row.processed_at is None
