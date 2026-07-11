"""BL-7 — session.implement absorbs background/resume/fresh (was session.delegate).

Tests verify:
  1. companion _parse_params("session.implement"): full flag set (marshaling)
  2. daemon session.implement handler: threads conversation_id/save_dir on --resume
     and leaves it unset on --fresh

RED before BL-7 changes; GREEN after.

Folded from tests/test_delegate_params.py — same behavior, same coverage,
now exercised through session.implement instead of session.delegate.
"""
from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from cao.runtime import session_store
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.daemon import handle_client
from cao.runtime.ipc import read_message, write_message
from cao.runtime.session_manager import SessionManager

_COMPANION_PATH = (
    Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "cao-companion.py"
)


def _load_companion() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cao_companion", _COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


companion = _load_companion()


# --- Companion marshaling: session.implement full flag set ------------------


def test_implement_no_flags_plain_task() -> None:
    """Baseline: no flags → same shape as before BL-7."""
    params = companion._parse_params("session.implement", "do the thing", "/ws")
    assert params == {"task": "do the thing", "workspace": "/ws"}


def test_implement_background_flag() -> None:
    params = companion._parse_params("session.implement", "ship it --background", "/ws")
    assert params == {"task": "ship it", "workspace": "/ws", "background": True}


def test_implement_resume_bare_resumes_latest() -> None:
    params = companion._parse_params("session.implement", "follow up --resume", "/ws")
    assert params == {"task": "follow up", "workspace": "/ws", "resume": True}


def test_implement_resume_with_id() -> None:
    params = companion._parse_params(
        "session.implement", "--resume sess-42 keep going", "/ws"
    )
    assert params == {
        "task": "keep going",
        "workspace": "/ws",
        "resume": True,
        "conversation_id": "sess-42",
    }


def test_implement_fresh_sets_resume_false() -> None:
    params = companion._parse_params("session.implement", "start over --fresh", "/ws")
    assert params == {"task": "start over", "workspace": "/ws", "resume": False}


def test_implement_fresh_beats_resume() -> None:
    params = companion._parse_params("session.implement", "go --resume --fresh", "/ws")
    assert params["resume"] is False
    assert "conversation_id" not in params


def test_implement_passes_all_flags() -> None:
    params = companion._parse_params(
        "session.implement",
        "--model gemini-2.5-flash --effort high --file a.png make it --background",
        "/ws",
    )
    assert params == {
        "task": "make it",
        "workspace": "/ws",
        "model": "gemini-2.5-flash",
        "effort": "high",
        "files": ["a.png"],
        "background": True,
    }


def test_implement_no_flags_matches_delegate_shape() -> None:
    """After BL-7, implement and delegate (when stripped) produce identical params."""
    impl = companion._parse_params("session.implement", "do the thing", "/ws")
    assert impl == {"task": "do the thing", "workspace": "/ws"}


# --- Companion marshaling: session.handoff --background (BL-19) --------------


def test_handoff_no_flags_plain_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: no flags → target is the raw text, no background key."""
    monkeypatch.delenv("CLAUDE_TRANSCRIPT_PATH", raising=False)
    params = companion._parse_params("session.handoff", "continue the refactor", "/ws")
    assert params == {"target": "continue the refactor", "workspace": "/ws"}


def test_handoff_background_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """--background is stripped from the target text and marshaled as a flag."""
    monkeypatch.delenv("CLAUDE_TRANSCRIPT_PATH", raising=False)
    params = companion._parse_params(
        "session.handoff", "continue the refactor --background", "/ws"
    )
    assert params == {
        "target": "continue the refactor",
        "workspace": "/ws",
        "background": True,
    }


# --- Config threading: daemon session.implement + session_manager -----------


def _capturing_mgr(captured: dict[str, Any]) -> SessionManager:
    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        captured["config"] = config
        agent = AsyncMock()
        resp = AsyncMock()
        resp.text = AsyncMock(return_value="ok")
        agent.chat = AsyncMock(return_value=resp)
        agent.conversation_id = "conv-new"
        yield agent

    return SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_factory)


async def _run_implement(
    tmp_path: Path, mgr: SessionManager, params: dict[str, Any]
) -> dict[str, Any]:
    sock = tmp_path / "d.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(
            r, w, asyncio.Event(), session_manager=mgr, approval_waiter=ApprovalWaiter()
        ),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await write_message(
            w,
            {"jsonrpc": "2.0", "id": 1, "method": "session.implement", "params": params},
        )
        resp = await read_message(r)
        w.close()
        await w.wait_closed()
    sid = resp["result"]["session_id"]
    task = mgr._tasks.get(sid)
    if task is not None:
        await task
    return resp


async def test_implement_resume_threads_stored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    ws = str(tmp_path)
    stored_dir = str(tmp_path / "stored-traj")
    session_store.record(ws, "old-sess", "conv-abc", stored_dir)

    captured: dict[str, Any] = {}
    mgr = _capturing_mgr(captured)
    await _run_implement(
        tmp_path, mgr, {"task": "again", "workspace": ws, "resume": True}
    )

    cfg = captured["config"]
    assert cfg.conversation_id == "conv-abc"
    assert cfg.save_dir == stored_dir


async def test_implement_fresh_ignores_stored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    ws = str(tmp_path)
    stored_dir = str(tmp_path / "stored-traj")
    session_store.record(ws, "old-sess", "conv-abc", stored_dir)

    captured: dict[str, Any] = {}
    mgr = _capturing_mgr(captured)
    await _run_implement(
        tmp_path, mgr, {"task": "new work", "workspace": ws, "resume": False}
    )

    cfg = captured["config"]
    assert cfg.conversation_id is None
    assert cfg.save_dir != stored_dir
    assert cfg.save_dir.endswith("/trajectories/" + _last_session_id(mgr))


def _last_session_id(mgr: SessionManager) -> str:
    return next(iter(mgr._sessions))
