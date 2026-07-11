from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.digest_generator import DigestGenerator
from cao.runtime.event_bus import EventBus
from cao.runtime.git_diff_collector import GitDiffCollector
from cao.runtime.hook_adapter import (
    CAOOnSessionEndHook,
    CAOOnSessionStartHook,
    CAOOnToolErrorHook,
    CAOPostToolCallHook,
    CAOPreToolCallDecideHook,
)
from cao.runtime.session_manager import SessionAlreadyActiveError, SessionManager


def _mgr(tmp_path: Path, **extra: Any) -> SessionManager:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    event_bus = EventBus(state_dir)
    return SessionManager(
        approval_waiter=ApprovalWaiter(),
        event_bus=event_bus,
        git_diff_collector=GitDiffCollector(state_dir),
        digest_generator=DigestGenerator(state_dir),
        **extra,
    )


async def test_build_agent_config_vertex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project-1")
    ws = str(tmp_path)
    cfg = _mgr(tmp_path).build_agent_config("sess-1", ws)

    assert cfg.workspaces == [ws]
    # We pass policies=[]; the SDK auto-prepends workspace_only, so this is > 0.
    assert len(cfg.policies) > 0
    assert len(cfg.hooks) == 5
    types = [type(h) for h in cfg.hooks]
    assert CAOPreToolCallDecideHook in types
    assert CAOPostToolCallHook in types
    assert CAOOnToolErrorHook in types
    assert CAOOnSessionStartHook in types
    assert CAOOnSessionEndHook in types
    assert getattr(cfg, "vertex", False) is True


async def test_build_agent_config_gemini_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    cfg = _mgr(tmp_path).build_agent_config("sess-2", str(tmp_path))
    assert getattr(cfg, "api_key", None) == "test-api-key"
    assert not getattr(cfg, "vertex", False)


async def test_build_agent_config_review_disables_mutating_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project-1")
    normal = _mgr(tmp_path).build_agent_config("s-n", str(tmp_path), review=False)
    assert normal.capabilities.disabled_tools is None

    review = _mgr(tmp_path).build_agent_config("s-r", str(tmp_path), review=True)
    disabled = {t.value for t in review.capabilities.disabled_tools}
    assert disabled == {"create_file", "edit_file", "run_command", "generate_image", "start_subagent"}


async def test_run_task_with_fake_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="hello from fake agent")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _fake_factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    mgr = _mgr(tmp_path, agent_factory=_fake_factory)
    session = await mgr.create_session("slug-1", str(tmp_path), "do something")
    result = await mgr.run_task(session.session_id, "ping")
    assert result == "hello from fake agent"


async def test_run_task_publishes_session_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_task publishes a session.response event carrying the worker's narration."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="WORKER_NARRATION_TEXT")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    mgr = _mgr(tmp_path, agent_factory=_factory)
    session = await mgr.create_session("slug-wr", str(tmp_path), "do")
    await mgr.run_task(session.session_id, "ping")
    events = await mgr.get_events(session.session_id)
    pairs = [(e.event_type, e.payload.get("text")) for e in events]
    assert ("session.response", "WORKER_NARRATION_TEXT") in pairs


async def test_create_session_returns_session() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-a", "/workspace", "task")
    assert session.state == "running"
    assert session.workspace == "/workspace"


async def test_broker_rejects_duplicate_active_session() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    await mgr.create_session("slug-b", "/workspace")
    with pytest.raises(SessionAlreadyActiveError):
        await mgr.create_session("slug-b", "/workspace")


async def test_get_session_unknown_returns_none() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    assert mgr.get_session("no-such-id") is None


async def test_cancel_session_transitions_and_releases_slug() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-c", "/workspace")
    await mgr.cancel_session(session.session_id)
    cancelled = mgr.get_session(session.session_id)
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    new_session = await mgr.create_session("slug-c", "/workspace")
    assert new_session.state == "running"


async def test_transition_updates_state() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-d", "/workspace")
    mgr.transition(session.session_id, "done")
    updated = mgr.get_session(session.session_id)
    assert updated is not None
    assert updated.state == "done"


async def test_start_task_success_transitions_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="ok")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    mgr = _mgr(tmp_path, agent_factory=_factory)
    session = await mgr.create_session("slug-ok", str(tmp_path), "do it")
    mgr.start_task(session.session_id, "do it")
    await mgr._tasks[session.session_id]
    done = mgr.get_session(session.session_id)
    assert done is not None
    assert done.state == "done"
    assert session.session_id not in mgr._tasks


async def test_start_task_crash_transitions_to_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    boom_agent = AsyncMock()
    boom_agent.chat = AsyncMock(side_effect=RuntimeError("worker exploded"))

    @asynccontextmanager
    async def _boom_factory(config: Any) -> AsyncGenerator[Any, None]:
        yield boom_agent

    mgr = _mgr(tmp_path, agent_factory=_boom_factory)
    session = await mgr.create_session("slug-boom", str(tmp_path), "explode")
    mgr.start_task(session.session_id, "explode")
    await mgr._tasks[session.session_id]
    crashed = mgr.get_session(session.session_id)
    assert crashed is not None
    assert crashed.state == "crashed"


async def test_latest_session_id_none_when_empty() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    assert mgr.latest_session_id() is None


async def test_latest_session_id_returns_most_recent() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    await mgr.create_session("slug-1", "/workspace", "task one")
    second = await mgr.create_session("slug-2", "/workspace", "task two")
    assert mgr.latest_session_id() == second.session_id
    # Survives completion: the retry target persists after leaving _active.
    mgr.transition(second.session_id, "crashed")
    assert mgr.latest_session_id() == second.session_id


async def test_active_session_id_none_when_no_active() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    assert mgr.active_session_id() is None
    session = await mgr.create_session("slug-x", "/workspace")
    await mgr.cancel_session(session.session_id)
    assert mgr.active_session_id() is None


async def test_active_session_id_returns_running() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-y", "/workspace")
    assert mgr.active_session_id() == session.session_id
    assert session.session_id not in mgr._tasks


async def test_list_sessions_returns_id_and_state() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    s1 = await mgr.create_session("slug-1", "/workspace", "one")
    s2 = await mgr.create_session("slug-2", "/workspace", "two")
    mgr.transition(s1.session_id, "done")
    by_id = {x["session_id"]: x["state"] for x in mgr.list_sessions()}
    assert by_id == {s1.session_id: "done", s2.session_id: "running"}


async def test_list_sessions_empty() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    assert mgr.list_sessions() == []


async def test_start_task_stashes_opts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="ok")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    mgr = _mgr(tmp_path, agent_factory=_factory)
    session = await mgr.create_session("slug-opts", str(tmp_path), "do")
    mgr.start_task(
        session.session_id,
        "do",
        model="gemini-3.5-flash",
        effort="high",
        files=["IMG"],
        conversation_id="c1",
    )
    await mgr._tasks[session.session_id]
    # Only provided keys are stashed (save_dir was not passed).
    assert mgr.get_task_opts(session.session_id) == {
        "model": "gemini-3.5-flash",
        "effort": "high",
        "files": ["IMG"],
        "conversation_id": "c1",
    }


async def test_get_task_opts_empty_for_unknown() -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    assert mgr.get_task_opts("no-such-id") == {}


async def test_get_task_opts_returns_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="ok")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    mgr = _mgr(tmp_path, agent_factory=_factory)
    session = await mgr.create_session("slug-copy", str(tmp_path), "do")
    mgr.start_task(session.session_id, "do", model="m1")
    await mgr._tasks[session.session_id]
    # Mutating the returned dict must not corrupt internal state — retry pops
    # conversation_id off this dict, so it has to be a copy.
    opts = mgr.get_task_opts(session.session_id)
    opts["model"] = "MUTATED"
    opts["extra"] = "x"
    assert mgr.get_task_opts(session.session_id) == {"model": "m1"}
