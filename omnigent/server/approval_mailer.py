"""Email the owner when an unattended session waits on an approval.

A session a subscription launched has nobody watching it: the approval card
is mirrored into the session and its ancestors, and a blocked sub-agent wakes
its parent agent, but no human sees either until they happen to open the app.

:class:`ApprovalEmailNotifier` observes the same elicitation chokepoint as
:class:`omnigent.runtime.subagent_block_notifier.SubagentBlockNotifier`. When
a prompt in a subscription-launched tree is still unanswered after a short
grace, it mails the root session's owner one message with the approval link,
if the owner has a verified email and the server has a mailer
(:mod:`omnigent.server.mailer`). One message per prompt; nothing is sent for
sessions a person started, since that person is the one watching.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING, Any

from omnigent.runtime.subagent_block_notifier import _block_reason

if TYPE_CHECKING:
    from omnigent.server.mailer import SmtpMailer
    from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

_REQUEST_TYPE = "response.elicitation_request"
_RESOLVED_TYPE = "response.elicitation_resolved"
# Label the subscription launcher stamps on the sessions it starts.
LAUNCHED_BY_LABEL = "omnigent.launched_by"
_UNATTENDED_LAUNCHERS = frozenset({"subscription"})
# An approval answered within this grace was seen; mail only the ones that were not.
APPROVAL_EMAIL_DELAY_S = 120.0


async def _grace_sleep(seconds: float) -> None:
    """Indirection over :func:`asyncio.sleep` so tests can skip the grace."""
    await asyncio.sleep(seconds)


def _is_ancestor_mirror(conversation_id: str, event: dict[str, Any]) -> bool:
    """Whether *event* is a child's prompt mirrored into ancestor *conversation_id*."""
    params = event.get("params")
    target = params.get("target_session_id") if isinstance(params, dict) else None
    return isinstance(target, str) and target != conversation_id


class ApprovalEmailNotifier:
    """
    Mail the owner of an unattended session about an unanswered approval.

    :param conversation_store: Resolves the blocked session, its root and
        the root's owner.
    :param account_store: Account lookup with ``get_user(user_id)`` returning
        an account carrying ``email`` and ``email_verified_at``.
    :param mailer: Delivers the message.
    :param base_url: Public web origin for the approval link, e.g.
        ``"https://app.wakevane.com"``.
    :param loop: The server's event loop; :meth:`observe` may run off it.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        account_store: Any,
        mailer: SmtpMailer,
        base_url: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._conversation_store = conversation_store
        self._account_store = account_store
        self._mailer = mailer
        self._base_url = base_url.rstrip("/")
        self._loop = loop
        self._lock = threading.Lock()
        self._pending: set[tuple[str, str]] = set()
        self._inflight: set[concurrent.futures.Future[None]] = set()

    def observe(self, conversation_id: str, event: dict[str, Any]) -> None:
        """Track one elicitation event (an elicitation observer; no I/O)."""
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        key = (conversation_id, elicitation_id)
        event_type = event.get("type")
        if event_type == _RESOLVED_TYPE:
            with self._lock:
                self._pending.discard(key)
            return
        if event_type != _REQUEST_TYPE or _is_ancestor_mirror(conversation_id, event):
            return
        with self._lock:
            if key in self._pending:
                return  # re-published prompt: one message per approval
            self._pending.add(key)
        try:
            future = asyncio.run_coroutine_threadsafe(self.handle_request(key, event), self._loop)
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)
            return
        with self._lock:
            self._inflight.add(future)
        future.add_done_callback(self._discard)

    def _discard(self, future: concurrent.futures.Future[None]) -> None:
        with self._lock:
            self._inflight.discard(future)
        if not future.cancelled() and future.exception() is not None:
            _logger.warning("approval email failed", exc_info=future.exception())

    def close(self) -> None:
        """Cancel outstanding grace timers. For lifespan teardown."""
        with self._lock:
            inflight = list(self._inflight)
            self._inflight.clear()
        for future in inflight:
            future.cancel()

    async def handle_request(self, key: tuple[str, str], event: dict[str, Any]) -> None:
        """Wait out the grace, then mail the owner if the prompt is still open."""
        await _grace_sleep(APPROVAL_EMAIL_DELAY_S)
        with self._lock:
            if key not in self._pending:
                return  # answered within the grace
        conversation_id, elicitation_id = key
        message = await asyncio.to_thread(self._compose, conversation_id, elicitation_id, event)
        if message is None:
            return
        to, subject, text = message
        with self._lock:
            if key not in self._pending:
                return
        await asyncio.to_thread(self._mailer.send, to=to, subject=subject, text=text)

    def _compose(
        self, conversation_id: str, elicitation_id: str, event: dict[str, Any]
    ) -> tuple[str, str, str] | None:
        """The (recipient, subject, body) for one prompt, or ``None`` to send nothing."""
        store = self._conversation_store
        blocked = store.get_conversation(conversation_id)
        if blocked is None:
            return None
        root = (
            blocked
            if blocked.root_conversation_id in (None, blocked.id)
            else store.get_conversation(blocked.root_conversation_id)
        )
        if root is None or root.labels.get(LAUNCHED_BY_LABEL) not in _UNATTENDED_LAUNCHERS:
            return None
        owner = store.get_session_owner(root.id, owner_only=True)
        account = self._account_store.get_user(owner) if owner else None
        email = getattr(account, "email", None)
        if not email or getattr(account, "email_verified_at", None) is None:
            _logger.info(
                "approval pending in unattended session %s; owner has no verified email",
                conversation_id,
            )
            return None
        title = blocked.title or conversation_id
        reason = _block_reason(event) or "A command needs your approval."
        link = f"{self._base_url}/approve/{conversation_id}/{elicitation_id}"
        session_link = f"{self._base_url}/c/{root.id}"
        text = (
            f"An agent session started by a subscription is waiting for you.\n\n"
            f"Session: {title}\n"
            f"Request: {reason}\n\n"
            f"Approve or deny: {link}\n"
            f"Open the session: {session_link}\n\n"
            "The session stays paused until you answer. You get one email per request.\n"
        )
        return email, f"Approval needed: {title}", text


def fan_out_observers(*observers: Any) -> Any:
    """One elicitation observer that calls each of *observers* in order.

    An observer raising is logged and does not stop the others.
    """

    def _observe(conversation_id: str, event: dict[str, Any]) -> None:
        for observer in observers:
            try:
                observer(conversation_id, event)
            except Exception:
                _logger.warning("elicitation observer failed", exc_info=True)

    return _observe
