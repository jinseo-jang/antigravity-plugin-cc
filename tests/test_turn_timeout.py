from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from cao.runtime import session_manager as sm_mod
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.digest_generator import DigestGenerator
from cao.runtime.event_bus import EventBus
from cao.runtime.git_diff_collector import GitDiffCollector
from cao.runtime.session_manager import SessionManager


def _mgr(tmp_path: Path, **extra: Any) -> SessionManager:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return SessionManager(
        approval_waiter=ApprovalWaiter(),
        event_bus=EventBus(state_dir),
        git_diff_collector=GitDiffCollector(state_dir),
        digest_generator=DigestGenerator(state_dir),
        **extra,
    )


async def test_slow_turn_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(sm_mod, "_TURN_TIMEOUT", 0.1)

    async def _slow_chat(_prompt: Any) -> Any:
        await asyncio.sleep(30)  # far beyond the 0.1s cap

    slow_agent = AsyncMock()
    slow_agent.chat = _slow_chat

    @asynccontextmanager
    async def _slow_factory(config: Any) -> AsyncGenerator[Any, None]:
        yield slow_agent

    mgr = _mgr(tmp_path, agent_factory=_slow_factory)
    session = await mgr.create_session("slug-slow", str(tmp_path), "hang")
    mgr.start_task(session.session_id, "hang")

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(mgr._tasks[session.session_id], timeout=5)
    elapsed = loop.time() - t0

    assert elapsed < 5, f"turn did not unwind at the cap (took {elapsed:.2f}s)"
    timed = mgr.get_session(session.session_id)
    assert timed is not None
    assert timed.state == "timed_out"
    assert session.session_id not in mgr._tasks
    assert session.session_id not in mgr._active.values()

    ej = tmp_path / "state" / "events.jsonl"
    events = [json.loads(ln) for ln in ej.read_text().splitlines() if ln.strip()]
    ended = [e for e in events if e.get("type") == "session.ended"]
    assert ended, f"no session.ended emitted; got {[e.get('type') for e in events]}"
    payload = ended[-1]["payload"]
    assert payload["status"] == "timed_out"
    assert "exceeded" in payload["reason"]


async def test_fast_turn_still_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(sm_mod, "_TURN_TIMEOUT", 5.0)

    fast_resp = AsyncMock()
    fast_resp.text = AsyncMock(return_value="ok")
    fast_agent = AsyncMock()
    fast_agent.chat = AsyncMock(return_value=fast_resp)

    @asynccontextmanager
    async def _fast_factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fast_agent

    mgr = _mgr(tmp_path, agent_factory=_fast_factory)
    session = await mgr.create_session("slug-fast", str(tmp_path), "quick")
    mgr.start_task(session.session_id, "quick")
    await mgr._tasks[session.session_id]

    done = mgr.get_session(session.session_id)
    assert done is not None
    assert done.state == "done"
    assert session.session_id not in mgr._tasks
