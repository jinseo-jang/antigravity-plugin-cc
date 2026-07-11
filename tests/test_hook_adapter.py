"""Tests for hook adapters — SDK-enforce-based CAOPreToolCallDecideHook."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from google.antigravity import types  # type: ignore[import-untyped]
from google.antigravity.hooks import policy as sdk_policy  # type: ignore[import-untyped]

from cao.models import ApprovalDecision, SECRET_PATH_MASK
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.hook_adapter import (
    CAOOnSessionEndHook,
    CAOOnSessionStartHook,
    CAOPostToolCallHook,
    CAOPreToolCallDecideHook,
    make_approval_handler,
)
from cao.runtime.policy_engine import PolicyEngine, WorkspaceConfig


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self) -> None:
        self._state: dict[str, Any] = {}

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


published: list[tuple[str, str, dict[str, Any]]] = []


async def _fake_bus(session_id: str, event_type: str, payload: dict[str, Any]) -> None:
    published.append((session_id, event_type, payload))


@pytest.fixture(autouse=True)
def _clear_published() -> None:
    published.clear()


def _ctx(session_id: str = "sess-1") -> _FakeContext:
    ctx = _FakeContext()
    ctx.set_state("session_id", session_id)
    return ctx


def _tc(name: str, canonical_path: str | None = None) -> types.ToolCall:
    return types.ToolCall(name=name, canonical_path=canonical_path)


# ---------------------------------------------------------------------------
# CAOPreToolCallDecideHook: publishes tool.requested, delegates to enforcer
# ---------------------------------------------------------------------------


async def test_hook_publishes_tool_requested() -> None:
    hook = CAOPreToolCallDecideHook(
        policies=[sdk_policy.allow("*")],
        event_bus=_fake_bus,
    )
    await hook.run(_ctx(), _tc("view_file", "/workspace/main.py"))
    event_types = [e[1] for e in published]
    assert "tool.requested" in event_types


async def test_hook_allow_via_sdk_policies() -> None:
    hook = CAOPreToolCallDecideHook(policies=[sdk_policy.allow("*")])
    result = await hook.run(_ctx(), _tc("view_file", "/workspace/main.py"))
    assert result.allow is True


async def test_hook_deny_via_sdk_policies() -> None:
    hook = CAOPreToolCallDecideHook(policies=[sdk_policy.deny("*")])
    result = await hook.run(_ctx(), _tc("view_file", "/workspace/main.py"))
    assert result.allow is False


# ---------------------------------------------------------------------------
# make_approval_handler: suspend/resume via ApprovalWaiter
# ---------------------------------------------------------------------------


async def test_make_approval_handler_approve() -> None:
    waiter = ApprovalWaiter()
    handler = make_approval_handler(waiter, _fake_bus, lambda: "sess-1")
    tc = _tc("run_command")
    task = asyncio.create_task(handler(tc))
    await asyncio.sleep(0)
    assert not task.done(), "handler should be suspended waiting for approval"
    # find call_id from published event
    req_events = [e for e in published if e[1] == "approval.required"]
    assert req_events, "approval.required must be published"
    call_id: str = req_events[0][2]["call_id"]
    waiter.resolve(call_id, ApprovalDecision.ALLOW)
    result = await task
    assert result is True


async def test_make_approval_handler_deny() -> None:
    waiter = ApprovalWaiter()
    handler = make_approval_handler(waiter, _fake_bus, lambda: "sess-1")
    task = asyncio.create_task(handler(_tc("run_command")))
    await asyncio.sleep(0)
    req_events = [e for e in published if e[1] == "approval.required"]
    call_id: str = req_events[0][2]["call_id"]
    waiter.resolve(call_id, ApprovalDecision.DENY)
    result = await task
    assert result is False


class _FakeSession:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace


class _FakeSessionManager:
    def __init__(self, workspace: str) -> None:
        self._ws = workspace
        self.transitions: list[str] = []

    def get_session(self, sid: str) -> _FakeSession:
        return _FakeSession(self._ws)

    def transition(self, sid: str, state: str) -> None:
        self.transitions.append(state)


async def test_make_approval_handler_auto_allows_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cao.runtime import approval_store

    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    approval_store.remember("ls -la", "/ws", "project")

    waiter = ApprovalWaiter()
    mgr = _FakeSessionManager("/ws")
    handler = make_approval_handler(
        waiter, _fake_bus, lambda: "sess-1", session_manager=mgr
    )
    tc = types.ToolCall(name="run_command", args={"command_line": "ls -la"})
    result = await handler(tc)
    assert result is True
    assert waiter.pending_ids == frozenset()
    assert mgr.transitions == []  # never suspended
    auto = [e for e in published if e[1] == "approval.auto_allowed"]
    assert auto and auto[0][2]["command"] == "ls -la"
    assert auto[0][2]["scope"] == "remembered"


async def test_make_approval_handler_prompts_when_not_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    waiter = ApprovalWaiter()
    mgr = _FakeSessionManager("/ws")
    handler = make_approval_handler(
        waiter, _fake_bus, lambda: "sess-1", session_manager=mgr
    )
    tc = types.ToolCall(name="run_command", args={"command_line": "rm -rf /"})
    task = asyncio.create_task(handler(tc))
    await asyncio.sleep(0)
    assert not task.done()
    assert waiter.pending_ids != frozenset()
    call_id = next(iter(waiter.pending_ids))
    waiter.resolve(call_id, ApprovalDecision.ALLOW)
    assert await task is True


async def test_make_approval_handler_timeout() -> None:
    waiter = ApprovalWaiter()
    handler = make_approval_handler(waiter, _fake_bus, lambda: "sess-1", timeout_seconds=0.02)
    result = await handler(_tc("run_command"))
    assert result is False


async def test_make_approval_handler_publishes_approval_required() -> None:
    waiter = ApprovalWaiter()
    handler = make_approval_handler(waiter, _fake_bus, lambda: "sess-1")
    task = asyncio.create_task(handler(_tc("run_command")))
    await asyncio.sleep(0)
    event_types = [e[1] for e in published]
    assert "approval.required" in event_types
    req_events = [e for e in published if e[1] == "approval.required"]
    call_id = req_events[0][2]["call_id"]
    waiter.resolve(call_id, ApprovalDecision.ALLOW)
    await task


# ---------------------------------------------------------------------------
# Integration: real PolicyEngine + hook for .env denial path
# ---------------------------------------------------------------------------


async def test_env_path_through_hook_returns_deny() -> None:
    engine = PolicyEngine()
    policies = engine.build_policies(WorkspaceConfig(workspace_root="/workspace"))
    hook = CAOPreToolCallDecideHook(policies=policies, event_bus=_fake_bus)
    result = await hook.run(_ctx(), _tc("view_file", "/workspace/.env"))
    assert result.allow is False


# ---------------------------------------------------------------------------
# BL-25: containment observability — deny/auto_allow events record the path
# ---------------------------------------------------------------------------


async def test_workspace_containment_deny_event_tagged_and_carries_real_path() -> None:
    engine = PolicyEngine()
    policies = engine.build_policies(WorkspaceConfig(workspace_root="/workspace"))
    hook = CAOPreToolCallDecideHook(policies=policies, event_bus=_fake_bus)
    await hook.run(_ctx(), _tc("view_file", "/etc/passwd"))
    denied = [e for e in published if e[1] == "tool.denied"]
    assert denied, "workspace-containment deny must publish tool.denied"
    assert denied[0][2]["reason"] == "workspace_containment"
    assert denied[0][2]["path"] == "/etc/passwd"


async def test_secret_file_deny_event_tagged_and_masks_path() -> None:
    engine = PolicyEngine()
    policies = engine.build_policies(WorkspaceConfig(workspace_root="/workspace"))
    hook = CAOPreToolCallDecideHook(policies=policies, event_bus=_fake_bus)
    await hook.run(_ctx(), _tc("view_file", "/workspace/.env"))
    denied = [e for e in published if e[1] == "tool.denied"]
    assert denied, "secret-file deny must publish tool.denied"
    assert denied[0][2]["reason"] == "deny_secrets"
    assert denied[0][2]["path"] == SECRET_PATH_MASK


async def test_review_deny_event_tagged_review_readonly_not_containment() -> None:
    """BL-25 (CONCERN-A): a read-only-review deny of an IN-workspace file is tagged
    review_readonly (keeps its real path), NOT workspace_containment — so the digest
    never miscounts it as an out-of-workspace breach."""
    engine = PolicyEngine()
    policies = engine.build_policies(WorkspaceConfig(workspace_root="/workspace", review=True))
    hook = CAOPreToolCallDecideHook(policies=policies, event_bus=_fake_bus)
    await hook.run(_ctx(), _tc("edit_file", "/workspace/main.py"))
    denied = [e for e in published if e[1] == "tool.denied"]
    assert denied, "review-mode mutating deny must publish tool.denied"
    assert denied[0][2]["reason"] == "review_readonly"
    assert denied[0][2]["path"] == "/workspace/main.py"


async def test_auto_allowed_event_carries_path() -> None:
    hook = CAOPreToolCallDecideHook(policies=[sdk_policy.allow("*")], event_bus=_fake_bus)
    await hook.run(_ctx(), _tc("view_file", "/workspace/main.py"))
    allowed = [e for e in published if e[1] == "tool.auto_allowed"]
    assert allowed, "allowed tool must publish tool.auto_allowed"
    assert allowed[0][2]["path"] == "/workspace/main.py"


# ---------------------------------------------------------------------------
# OnSessionStartHook sets state on context
# ---------------------------------------------------------------------------


async def test_session_start_hook_sets_state() -> None:
    hook = CAOOnSessionStartHook(
        session_id="sess-42",
        workspace_path="/workspace",
        event_bus=_fake_bus,
    )
    ctx = _FakeContext()
    await hook.run(ctx, None)
    assert ctx.get_state("session_id") == "sess-42"
    assert ctx.get_state("workspace_path") == "/workspace"


async def test_session_start_hook_publishes_event() -> None:
    hook = CAOOnSessionStartHook(
        session_id="sess-42",
        workspace_path="/workspace",
        event_bus=_fake_bus,
    )
    await hook.run(_FakeContext(), None)
    event_types = [e[1] for e in published]
    assert "session.started" in event_types


# ---------------------------------------------------------------------------
# PostToolCallHook publishes tool.completed
# ---------------------------------------------------------------------------


async def test_post_tool_call_publishes_completed() -> None:
    hook = CAOPostToolCallHook(event_bus=_fake_bus)
    ctx = _FakeContext()
    ctx.set_state("session_id", "sess-1")
    ctx.set_state("call_id", "call-1")
    await hook.run(ctx, None)
    event_types = [e[1] for e in published]
    assert "tool.completed" in event_types


# ---------------------------------------------------------------------------
# OnSessionEndHook publishes session.ended
# ---------------------------------------------------------------------------


async def test_session_end_hook_publishes_event() -> None:
    hook = CAOOnSessionEndHook(event_bus=_fake_bus)
    ctx = _FakeContext()
    ctx.set_state("session_id", "sess-1")
    await hook.run(ctx, None)
    event_types = [e[1] for e in published]
    assert "session.ended" in event_types


# ---------------------------------------------------------------------------
# Daemon tests (unchanged)
# ---------------------------------------------------------------------------


async def test_daemon_session_implement_returns_session_id(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm

    sock = tmp_path / "d.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    from cao.runtime.session_manager import SessionManager

    mgr = SessionManager(approval_waiter=waiter)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.implement",
                     "params": {"slug": "test", "workspace": "/tmp", "task": "t"}})
        resp = await read_message(r)
        assert resp["result"]["status"] == "started"
        assert resp["result"]["session_id"]
        w.close()
        await w.wait_closed()


async def test_daemon_approve_unknown_call_id_returns_32602(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm

    sock = tmp_path / "d2.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    from cao.runtime.session_manager import SessionManager

    mgr = SessionManager(approval_waiter=waiter)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 2, "method": "session.approve",
                     "params": {"call_id": "no-such-id"}})
        resp = await read_message(r)
        assert resp["error"]["code"] == -32602
        w.close()
        await w.wait_closed()


async def test_daemon_approve_and_deny_round_trip(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm

    sock = tmp_path / "d3.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    from cao.runtime.session_manager import SessionManager

    mgr = SessionManager(approval_waiter=waiter)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))

        future = waiter.register_pending("test-call-1")

        await wm(w, {"jsonrpc": "2.0", "id": 3, "method": "session.approve",
                     "params": {"call_id": "test-call-1"}})
        resp = await read_message(r)
        assert resp["result"] == {"approved": True}
        assert await future == ApprovalDecision.ALLOW

        future2 = waiter.register_pending("test-call-2")
        await wm(w, {"jsonrpc": "2.0", "id": 4, "method": "session.deny",
                     "params": {"call_id": "test-call-2"}})
        resp2 = await read_message(r)
        assert resp2["result"] == {"denied": True}
        assert await future2 == ApprovalDecision.DENY

        w.close()
        await w.wait_closed()


async def test_daemon_status_includes_pending_approvals(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm
    from cao.runtime.session_manager import SessionManager

    sock = tmp_path / "dstat.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    mgr = SessionManager(approval_waiter=waiter)
    session = await mgr.create_session("slug-stat", "/tmp", "task")
    waiter.register_pending("1", command="touch foo.sh", session_id=session.session_id)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.status",
                     "params": {"session_id": session.session_id}})
        resp = await read_message(r)
        assert resp["result"]["state"] == "running"
        assert resp["result"]["pending_approvals"] == [{"call_id": "1", "command": "touch foo.sh"}]
        w.close()
        await w.wait_closed()


async def test_daemon_session_wait_returns_approval(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm
    from cao.runtime.session_manager import SessionManager

    sock = tmp_path / "dwait1.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    mgr = SessionManager(approval_waiter=waiter)
    session = await mgr.create_session("slug-wait", "/tmp", "task")
    waiter.register_pending("1", command="touch foo.sh", session_id=session.session_id)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.wait",
                     "params": {"session_id": session.session_id}})
        resp = await read_message(r)
        assert resp["result"]["kind"] == "approval"
        assert resp["result"]["pending_approvals"][0]["call_id"] == "1"
        w.close()
        await w.wait_closed()


async def test_daemon_approve_with_scope_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cao.runtime import approval_store
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm
    from cao.runtime.session_manager import SessionManager

    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    sock = tmp_path / "dscope.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    mgr = SessionManager(approval_waiter=waiter)
    session = await mgr.create_session("slug-scope", "/ws", "task")
    fut = waiter.register_pending("1", command="ls -la", session_id=session.session_id)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.approve",
                     "params": {"call_id": "1", "scope": "project"}})
        resp = await read_message(r)
        assert resp["result"] == {"approved": True}
        assert await fut == ApprovalDecision.ALLOW
        assert approval_store.is_allowed("ls -la", "/ws") is True
        w.close()
        await w.wait_closed()


async def test_daemon_approve_default_scope_no_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cao.runtime import approval_store
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm
    from cao.runtime.session_manager import SessionManager

    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    sock = tmp_path / "dscope2.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    mgr = SessionManager(approval_waiter=waiter)
    session = await mgr.create_session("slug-scope2", "/ws", "task")
    waiter.register_pending("1", command="ls -la", session_id=session.session_id)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.approve",
                     "params": {"call_id": "1"}})
        resp = await read_message(r)
        assert resp["result"] == {"approved": True}
        assert approval_store.is_allowed("ls -la", "/ws") is False
        w.close()
        await w.wait_closed()


async def test_daemon_session_wait_returns_done(tmp_path: Path) -> None:
    from cao.runtime.daemon import handle_client
    from cao.runtime.ipc import read_message, write_message as wm
    from cao.runtime.session_manager import SessionManager

    sock = tmp_path / "dwait2.sock"
    evt = asyncio.Event()
    waiter = ApprovalWaiter()
    mgr = SessionManager(approval_waiter=waiter)
    session = await mgr.create_session("slug-wait2", "/tmp", "task")
    mgr.transition(session.session_id, "done")
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await wm(w, {"jsonrpc": "2.0", "id": 1, "method": "session.wait",
                     "params": {"session_id": session.session_id}})
        resp = await read_message(r)
        assert resp["result"]["kind"] == "done"
        assert resp["result"]["state"] == "done"
        w.close()
        await w.wait_closed()
