"""Transactional ownership and immutable snapshots for packaged design reviews."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent, current_workspace_id
from omnigent.db.enum_codecs import encode_agent_kind
from omnigent.db.utils import builtin_agent_id, now_epoch, run_write_transaction
from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError

EGM_NAME = "egm-review"
TEMPLATE = "egm-template"
SNAPSHOT = "egm-session"


def lock_egm(session: Session) -> None:
    """Pair PostgreSQL advisory locking with the store's SQLite immediate session."""
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(f"{current_workspace_id()}:{EGM_NAME}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def mutable_agent(row: SqlAgent | None) -> None:
    if row is not None and row.managed_by == SNAPSHOT:
        raise OmnigentError(
            "Design review snapshots are immutable; create an editable copy or a new run",
            code=ErrorCode.CONFLICT,
        )


class EgmAgentStoreMixin:
    """Internal operations; markers are never accepted from bundle/API inputs."""

    _engine: Engine
    _session: Any
    _session_id_for_agent: Any
    _session_immediate: Any
    egm_bundle_location: str | None = None

    @property
    def supports_egm(self) -> bool:
        return self._engine.dialect.name in {"sqlite", "postgresql"}

    def egm_available(self, agent: Agent) -> bool:
        return bool(
            self.egm_bundle_location
            and agent.managed_by == TEMPLATE
            and agent.name == EGM_NAME
            and agent.id == builtin_agent_id(EGM_NAME)
            and agent.bundle_location == self.egm_bundle_location
        )

    def seed_egm(self, bundle_location: str, expected: Agent | None) -> Agent | None:
        """Compare the observed revision and claim only an empty template identity."""
        if not self.supports_egm:
            return None

        def write(session: Session) -> Agent | None:
            lock_egm(session)
            agent_id = builtin_agent_id(EGM_NAME)
            rows = list(
                session.scalars(
                    select(SqlAgent)
                    .where(
                        SqlAgent.workspace_id == current_workspace_id(),
                        or_(SqlAgent.name == EGM_NAME, SqlAgent.id == agent_id),
                        or_(SqlAgent.managed_by.is_(None), SqlAgent.managed_by != SNAPSHOT),
                    )
                    .with_for_update()
                )
            )
            if rows:
                if len(rows) != 1 or expected is None:
                    return None
                row = rows[0]
                if (row.id, row.name, row.managed_by, row.version, row.bundle_location) != (
                    agent_id,
                    EGM_NAME,
                    TEMPLATE,
                    expected.version,
                    expected.bundle_location,
                ):
                    return None
                if row.bundle_location != bundle_location:
                    row.bundle_location = bundle_location
                    row.version += 1
                    row.updated_at = now_epoch()
            else:
                if expected is not None:
                    return None
                row = SqlAgent(
                    id=agent_id,
                    name=EGM_NAME,
                    created_at=now_epoch(),
                    bundle_location=bundle_location,
                    version=1,
                    kind=encode_agent_kind("server"),
                    managed_by=TEMPLATE,
                    description=(
                        "Design a change with independent comprehension, "
                        "critic and readiness reviews"
                    ),
                )
                session.add(row)
            return sql_agent_to_entity(row)

        return run_write_transaction(self._session_immediate, "seed_egm", write)

    def create_egm_snapshot(
        self, expected: Agent, agent_id: str, bundle_location: str, owner: str | None
    ) -> Agent:
        """Admit against a current template revision and pin an independent artifact."""

        def write(session: Session) -> Agent:
            lock_egm(session)
            row = session.get(SqlAgent, (current_workspace_id(), expected.id))
            if (
                row is None
                or not self.egm_available(sql_agent_to_entity(row))
                or (row.version, row.bundle_location)
                != (expected.version, expected.bundle_location)
            ):
                raise OmnigentError(
                    "Design review is unavailable; reload the agent picker",
                    code=ErrorCode.CONFLICT,
                )
            snapshot = SqlAgent(
                id=agent_id,
                name=EGM_NAME,
                created_at=now_epoch(),
                bundle_location=bundle_location,
                version=1,
                kind=encode_agent_kind("user"),
                managed_by=SNAPSHOT,
                created_by=owner,
                description=row.description,
            )
            session.add(snapshot)
            return sql_agent_to_entity(snapshot)

        return run_write_transaction(self._session_immediate, "snapshot_egm", write)

    def abandoned_egm_snapshots(self, before: int) -> list[Agent]:
        """Find old snapshots with no surviving root or child reference."""
        with self._session("find_abandoned_egm_snapshots") as session:
            rows = list(
                session.scalars(
                    select(SqlAgent).where(
                        SqlAgent.workspace_id == current_workspace_id(),
                        SqlAgent.managed_by == SNAPSHOT,
                        SqlAgent.created_at < before,
                    )
                )
            )
        return [
            sql_agent_to_entity(row) for row in rows if self._session_id_for_agent(row.id) is None
        ]
