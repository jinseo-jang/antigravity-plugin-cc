"""asyncio.Future registry for pending tool-call approvals."""

from __future__ import annotations

import asyncio
import logging

from cao.models import ApprovalDecision

logger = logging.getLogger("cao.approval")


class UnknownCallIdError(Exception):
    """Raised by ApprovalWaiter.resolve when call_id is not in the registry."""


class ApprovalWaiter:
    """Registry of asyncio.Future[ApprovalDecision] keyed by call_id.

    Single asyncio event loop only — see concurrency_model.md.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._meta: dict[str, tuple[str, str]] = {}  # call_id -> (command, session_id)
        self._counter: int = 0

    def next_call_id(self) -> str:
        """Return a short, typeable, daemon-unique call id ('1', '2', ...)."""
        self._counter += 1
        return str(self._counter)

    def register_pending(
        self, call_id: str, command: str = "", session_id: str = ""
    ) -> asyncio.Future[ApprovalDecision]:
        """Create and register an unresolved Future for *call_id*."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[call_id] = future
        self._meta[call_id] = (command, session_id)
        logger.debug("registered pending call_id=%s", call_id)
        return future

    def resolve(self, call_id: str, decision: ApprovalDecision) -> None:
        """Resolve the pending Future for *call_id* — synchronous, no await.

        Atomic pop + set_result per concurrency_model.md §5 Rule 3.
        Raises UnknownCallIdError if call_id is not in the registry.
        Idempotent: if future is already done (e.g. timed out), returns silently.
        """
        future = self._pending.pop(call_id, None)
        self._meta.pop(call_id, None)
        if future is None:
            raise UnknownCallIdError(call_id)
        if future.done():
            return
        future.set_result(decision)
        logger.debug("resolved call_id=%s → %s", call_id, decision)

    def remove_if_pending(self, call_id: str) -> None:
        """Remove call_id from the registry without resolving (used after timeout)."""
        self._pending.pop(call_id, None)
        self._meta.pop(call_id, None)

    def cancel_all(self) -> None:
        """Cancel all outstanding Futures and clear the registry."""
        for call_id, future in list(self._pending.items()):
            if not future.done():
                future.cancel()
                logger.debug("cancelled pending call_id=%s", call_id)
        self._pending.clear()
        self._meta.clear()

    def meta_for(self, call_id: str) -> tuple[str, str] | None:
        """Return (command, session_id) for a pending call_id, or None."""
        return self._meta.get(call_id)

    @property
    def pending_ids(self) -> frozenset[str]:
        return frozenset(self._pending)

    def pending_details(self, session_id: str | None = None) -> list[dict[str, str]]:
        """Pending approvals as [{call_id, command}], optionally filtered by session."""
        out: list[dict[str, str]] = []
        for call_id in self._pending:
            command, sid = self._meta.get(call_id, ("", ""))
            if session_id is None or sid == session_id:
                out.append({"call_id": call_id, "command": command})
        return out
