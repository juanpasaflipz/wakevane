"""SQLAlchemy persistence for ladders and their attempts.

Requests read and write inside the caller's workspace. The event engine runs
outside any request, so the methods it uses (``list_active``,
``get_attempt_any_workspace``, ``list_running``, ``finish_attempt`` …) address
rows by id across workspaces, like the subscription store's bus-side methods.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, asc, desc, select, update
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlLadder,
    SqlLadderAttempt,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.server.ladders.model import AttemptStatus, Ladder, LadderAttempt, LadderState, Rung

_logger = logging.getLogger(__name__)


def _decode_object(raw: str | None) -> dict[str, Any]:
    """Decode JSON object text, reading empty or malformed text as ``{}``."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        _logger.warning("Ignoring malformed ladder JSON: %r", raw[:200])
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_rungs(raw: str) -> tuple[Rung, ...]:
    """Decode the stored rung list, dropping entries without an agent."""
    try:
        decoded = json.loads(raw)
    except ValueError:
        _logger.warning("Ignoring malformed ladder rungs JSON: %r", raw[:200])
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        Rung(
            agent_id=entry["agent_id"],
            model_override=entry.get("model_override"),
            max_cost_usd=entry.get("max_cost_usd"),
            label=entry.get("label"),
        )
        for entry in decoded
        if isinstance(entry, dict) and isinstance(entry.get("agent_id"), str)
    )


def _to_ladder(row: SqlLadder) -> Ladder:
    return Ladder(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        repo=row.repo,
        trigger_kind=row.trigger_kind,
        prompt_template=row.prompt_template,
        rungs=_decode_rungs(row.rungs),
        created_at=row.created_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        account_generation=row.account_generation,
        trigger_match=_decode_object(row.trigger_match),
        verify_kind=row.verify_kind,
        verify_name=row.verify_name,
        execution_target=row.execution_target,
        host_id=row.host_id,
        workspace=row.workspace,
        attempt_timeout_minutes=row.attempt_timeout_minutes,
        state=row.state,
        updated_at=row.updated_at,
    )


def _to_attempt(row: SqlLadderAttempt) -> LadderAttempt:
    return LadderAttempt(
        id=row.id,
        ladder_id=row.ladder_id,
        task_id=row.task_id,
        user_id=row.user_id,
        repo=row.repo,
        task_key=row.task_key,
        rung_index=row.rung_index,
        agent_id=row.agent_id,
        status=row.status,
        started_at=row.started_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        model_override=row.model_override,
        model=row.model,
        conversation_id=row.conversation_id,
        pull_request=row.pull_request,
        detail=row.detail,
        event_fields=_decode_object(row.event_fields),
        cost_usd=row.cost_usd,
        finished_at=row.finished_at,
    )


def _owned_by(column: Any, user_id: str | None) -> Any:
    """Owner filter; NULL never equals NULL, so single-user rows need IS NULL."""
    return column.is_(None) if user_id is None else column == user_id


class LadderStore:
    """
    Ladders and ladder attempts in the server database.

    :param storage_location: SQLAlchemy database URI shared with the other
        server stores, e.g. ``"sqlite:///chat.db"``.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.ladder_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.ladder_store", immediate=True
        )

    # ── Ladders ───────────────────────────────────────────────────

    def create_ladder(
        self,
        ladder_id: str,
        *,
        user_id: str | None,
        name: str,
        repo: str,
        trigger_kind: str,
        trigger_match: dict[str, Any],
        prompt_template: str,
        rungs: Sequence[Rung],
        verify_kind: str,
        verify_name: str | None,
        execution_target: str,
        host_id: str | None,
        workspace: str | None,
        attempt_timeout_minutes: int,
    ) -> Ladder:
        """Insert a new active ladder owned by ``user_id``."""
        created_at = now_epoch()

        def write(session: Session) -> Ladder:
            generation = require_active_account(session, user_id)
            row = SqlLadder(
                id=ladder_id,
                user_id=user_id,
                account_generation=generation,
                name=name,
                repo=repo,
                trigger_kind=trigger_kind,
                trigger_match=json.dumps(trigger_match, sort_keys=True),
                prompt_template=prompt_template,
                rungs=json.dumps([rung.to_json() for rung in rungs]),
                verify_kind=verify_kind,
                verify_name=verify_name,
                execution_target=execution_target,
                host_id=host_id,
                workspace=workspace,
                attempt_timeout_minutes=attempt_timeout_minutes,
                state=LadderState.ACTIVE.value,
                created_at=created_at,
                updated_at=None,
            )
            session.add(row)
            session.flush()
            return _to_ladder(row)

        return run_write_transaction(self._session_immediate, "insert_ladder", write)

    def get_ladder(self, ladder_id: str) -> Ladder | None:
        """Return a ladder in the caller's workspace, or ``None``."""
        with self._session("select_ladder_by_id") as session:
            row = session.get(SqlLadder, (current_workspace_id(), ladder_id))
            return _to_ladder(row) if row is not None else None

    def get_ladder_any_workspace(self, ladder_id: str) -> Ladder | None:
        """Return a ladder by id regardless of the request workspace."""
        with self._session("select_ladder_for_event") as session:
            row = (
                session.execute(select(SqlLadder).where(SqlLadder.id == ladder_id))
                .scalars()
                .first()
            )
            return _to_ladder(row) if row is not None else None

    def list_ladders_for_user(self, user_id: str | None) -> list[Ladder]:
        """A user's non-deleted ladders, oldest first."""
        with self._session("list_ladders_for_user") as session:
            stmt = (
                select(SqlLadder)
                .where(SqlLadder.workspace_id == current_workspace_id())
                .where(SqlLadder.state != LadderState.DELETED.value)
                .where(_owned_by(SqlLadder.user_id, user_id))
                .order_by(asc(SqlLadder.created_at), asc(SqlLadder.id))
            )
            return [_to_ladder(row) for row in session.execute(stmt).scalars().all()]

    def list_active(self, trigger_kind: str) -> list[Ladder]:
        """Active ladders triggered by one event kind, across every workspace."""
        with self._session("list_active_ladders_for_event") as session:
            stmt = (
                select(SqlLadder)
                .where(SqlLadder.state == LadderState.ACTIVE.value)
                .where(SqlLadder.trigger_kind == trigger_kind)
                .order_by(
                    asc(SqlLadder.workspace_id), asc(SqlLadder.created_at), asc(SqlLadder.id)
                )
            )
            return [_to_ladder(row) for row in session.execute(stmt).scalars().all()]

    def set_ladder_state(self, ladder_id: str, state: str) -> Ladder | None:
        """Set a ladder's lifecycle state; ``None`` when it is missing or deleted."""
        if state not in {member.value for member in LadderState}:
            raise ValueError(f"unknown ladder state {state!r}")

        def write(session: Session) -> Ladder | None:
            row = session.get(SqlLadder, (current_workspace_id(), ladder_id), with_for_update=True)
            if row is None or row.state == LadderState.DELETED.value:
                return None
            if row.state != state:
                row.state = state
                row.updated_at = now_epoch()
            session.flush()
            return _to_ladder(row)

        return run_write_transaction(self._session_immediate, "update_ladder_state", write)

    # ── Attempts ──────────────────────────────────────────────────

    def create_attempt(
        self,
        attempt_id: str,
        *,
        ladder: Ladder,
        task_id: str,
        task_key: str,
        rung_index: int,
        event_fields: dict[str, Any],
        started_at: int,
    ) -> LadderAttempt:
        """Record a running attempt for one rung of ``ladder``."""
        rung = ladder.rungs[rung_index]

        def write(session: Session) -> LadderAttempt:
            row = SqlLadderAttempt(
                workspace_id=ladder.workspace_id,
                id=attempt_id,
                ladder_id=ladder.id,
                task_id=task_id,
                user_id=ladder.user_id,
                repo=ladder.repo,
                task_key=task_key,
                rung_index=rung_index,
                agent_id=rung.agent_id,
                model_override=rung.model_override,
                status=AttemptStatus.RUNNING.value,
                event_fields=json.dumps(event_fields, sort_keys=True, default=str),
                started_at=started_at,
            )
            session.add(row)
            session.flush()
            return _to_attempt(row)

        return run_write_transaction(self._session_immediate, "insert_ladder_attempt", write)

    def get_attempt_any_workspace(self, attempt_id: str) -> LadderAttempt | None:
        """Return an attempt by id regardless of the request workspace."""
        with self._session("select_ladder_attempt_for_event") as session:
            row = (
                session.execute(select(SqlLadderAttempt).where(SqlLadderAttempt.id == attempt_id))
                .scalars()
                .first()
            )
            return _to_attempt(row) if row is not None else None

    def has_running_attempt(self, ladder_id: str, task_key: str) -> bool:
        """Whether a task for ``task_key`` is already being worked on."""
        with self._session("select_running_ladder_attempt_for_task") as session:
            stmt = (
                select(SqlLadderAttempt.id)
                .where(SqlLadderAttempt.ladder_id == ladder_id)
                .where(SqlLadderAttempt.task_key == task_key)
                .where(SqlLadderAttempt.status == AttemptStatus.RUNNING.value)
                .limit(1)
            )
            return session.execute(stmt).first() is not None

    def list_running(self) -> list[LadderAttempt]:
        """Every running attempt, oldest first, across every workspace."""
        with self._session("list_running_ladder_attempts") as session:
            stmt = (
                select(SqlLadderAttempt)
                .where(SqlLadderAttempt.status == AttemptStatus.RUNNING.value)
                .order_by(asc(SqlLadderAttempt.started_at), asc(SqlLadderAttempt.id))
            )
            return [_to_attempt(row) for row in session.execute(stmt).scalars().all()]

    def list_attempts_for_ladder(self, ladder_id: str, *, limit: int) -> list[LadderAttempt]:
        """A ladder's attempts in the caller's workspace, newest first."""
        with self._session("list_ladder_attempts_for_ladder") as session:
            stmt = (
                select(SqlLadderAttempt)
                .where(SqlLadderAttempt.workspace_id == current_workspace_id())
                .where(SqlLadderAttempt.ladder_id == ladder_id)
                # Rungs of one task can start in the same second.
                .order_by(
                    desc(SqlLadderAttempt.started_at),
                    desc(SqlLadderAttempt.rung_index),
                    desc(SqlLadderAttempt.id),
                )
                .limit(limit)
            )
            return [_to_attempt(row) for row in session.execute(stmt).scalars().all()]

    def list_attempts_for_user(
        self, user_id: str | None, *, started_since: int, repo: str | None = None
    ) -> list[LadderAttempt]:
        """A user's attempts started at or after ``started_since``, oldest first."""
        with self._session("list_ladder_attempts_for_scoreboard") as session:
            stmt = (
                select(SqlLadderAttempt)
                .where(SqlLadderAttempt.workspace_id == current_workspace_id())
                .where(_owned_by(SqlLadderAttempt.user_id, user_id))
                .where(SqlLadderAttempt.started_at >= started_since)
                .order_by(asc(SqlLadderAttempt.started_at), asc(SqlLadderAttempt.id))
            )
            if repo is not None:
                stmt = stmt.where(SqlLadderAttempt.repo == repo)
            return [_to_attempt(row) for row in session.execute(stmt).scalars().all()]

    def set_conversation(self, attempt_id: str, conversation_id: str) -> None:
        """Record the session an attempt runs in."""
        self._update_attempt(
            "update_ladder_attempt_conversation", attempt_id, conversation_id=conversation_id
        )

    def set_pull_request(self, attempt_id: str, pull_request: int) -> None:
        """Record the pull request an attempt opened."""
        self._update_attempt(
            "update_ladder_attempt_pull_request", attempt_id, pull_request=pull_request
        )

    def _update_attempt(self, query_name: str, attempt_id: str, **values: Any) -> None:
        def write(session: Session) -> None:
            session.execute(
                update(SqlLadderAttempt).where(SqlLadderAttempt.id == attempt_id).values(**values)
            )

        run_write_transaction(self._session_immediate, query_name, write)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        finished_at: int,
        detail: str | None = None,
        cost_usd: float | None = None,
        model: str | None = None,
    ) -> bool:
        """
        End a running attempt.

        The update only applies while the attempt is still running, so two
        deliveries of the same check result cannot both end it.

        :returns: Whether this call ended the attempt.
        """

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SqlLadderAttempt)
                    .where(SqlLadderAttempt.id == attempt_id)
                    .where(SqlLadderAttempt.status == AttemptStatus.RUNNING.value)
                    .values(
                        status=status,
                        finished_at=finished_at,
                        detail=detail[:1024] if detail else None,
                        cost_usd=cost_usd,
                        model=model,
                    )
                ),
            )
            return result.rowcount == 1

        return run_write_transaction(self._session_immediate, "finish_ladder_attempt", write)
