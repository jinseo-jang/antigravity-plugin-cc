"""Async pub/sub EventBus with append-only JSONL persistence.

event_bus_and_persistence.md §5-§8 implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cao.models import RuntimeEvent

logger = logging.getLogger("cao.event_bus")

Subscriber = Callable[[RuntimeEvent], Awaitable[None]]


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def handle(self, event: RuntimeEvent) -> None:
        line = json.dumps(event.model_dump(by_alias=True))
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._sync_append, line)

    def _sync_append(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


class EventBus:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._jsonl = _JsonlWriter(state_dir / "events.jsonl")
        # JSONL writer always first (durability before digest accumulation)
        self._subscribers: list[Subscriber] = [self._jsonl.handle]
        self._counters: dict[str, int] = {}  # session_id -> last assigned id

    def subscribe(self, handler: Subscriber) -> None:
        self._subscribers.append(handler)

    async def publish(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> RuntimeEvent:
        next_id = self._counters.get(session_id, 0) + 1
        self._counters[session_id] = next_id

        event = RuntimeEvent.model_validate(
            {
                "id": next_id,
                "session_id": session_id,
                "type": event_type,
                "timestamp_utc": datetime.now(timezone.utc),
                "payload": payload,
            }
        )
        logger.debug(
            "publish id=%d session_id=%s type=%s", next_id, session_id, event_type
        )

        for sub in self._subscribers:
            try:
                await sub(event)
            except Exception:
                logger.exception(
                    "subscriber %r raised on event id=%d session=%s",
                    sub,
                    next_id,
                    session_id,
                )

        return event

    async def get_events(
        self, session_id: str, after_event_id: int = 0
    ) -> list[RuntimeEvent]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._read_events, session_id, after_event_id
        )

    def _read_events(
        self, session_id: str, after_event_id: int
    ) -> list[RuntimeEvent]:
        path = self._state_dir / "events.jsonl"
        if not path.exists():
            return []
        events: list[RuntimeEvent] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                event = RuntimeEvent.model_validate(data)
                if event.session_id == session_id and event.id > after_event_id:
                    events.append(event)
            except Exception:
                pass  # ponytail: skip corrupt/partial line; crash recovery per §9
        return events

    def init_session_counter(self, session_id: str) -> None:
        """Resume monotonic counter from events.jsonl after daemon restart (§9)."""
        path = self._state_dir / "events.jsonl"
        if not path.exists():
            return
        max_id = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if data.get("session_id") == session_id:
                    max_id = max(max_id, int(data.get("id", 0)))
            except Exception:
                pass
        if max_id > 0:
            self._counters[session_id] = max_id


def make_publish_wrapper(
    bus: EventBus,
) -> Callable[[str, str, dict[str, Any]], Awaitable[None]]:
    """Return an AsyncPublish-compatible wrapper around EventBus.publish."""

    async def _wrapper(
        session_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        await bus.publish(session_id, event_type, payload)

    return _wrapper
