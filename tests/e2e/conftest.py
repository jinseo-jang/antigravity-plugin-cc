"""E2E fixtures: in-process daemon, real runtime components, fake-agent factory."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.daemon import compute_state_dir, handle_client
from cao.runtime.digest_generator import DigestGenerator
from cao.runtime.event_bus import EventBus, make_publish_wrapper
from cao.runtime.git_diff_collector import GitDiffCollector
from cao.runtime.hook_adapter import (
    CAOOnSessionEndHook,
    CAOOnSessionStartHook,
    CAOPostToolCallHook,
    CAOPreToolCallDecideHook,
)
from cao.runtime.policy_engine import PolicyEngine, WorkspaceConfig
from cao.runtime.session_manager import SessionManager
from tests.e2e.fake_sdk import FakeAgent


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


@pytest.fixture
async def git_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "config", "user.email", "test@example.com")
    _git(ws, "config", "user.name", "Test")
    (ws / "config.py").write_text("# initial")
    _git(ws, "add", "config.py")
    _git(ws, "commit", "-m", "initial")
    yield ws


@pytest.fixture
async def daemon_ctx(git_workspace: Path):
    """All real runtime components + in-process asyncio Unix socket server."""
    state_dir = compute_state_dir(git_workspace)
    sock_path = state_dir / "rpc.sock"

    waiter = ApprovalWaiter()
    event_bus = EventBus(state_dir)
    git_diff = GitDiffCollector(state_dir)
    digest_gen = DigestGenerator(state_dir)
    publish = make_publish_wrapper(event_bus)
    event_bus.subscribe(digest_gen.handle)

    policy = PolicyEngine(approval_waiter=waiter)
    policy.build_policies(WorkspaceConfig(workspace_root=str(git_workspace)))

    # e2e drives the real hooks manually via fake_agent, so the daemon's own
    # background run_task must stay inert (block until cancelled), not run Gemini.
    class _IdleAgent:
        def __init__(self, config: object = None) -> None:
            pass

        async def __aenter__(self) -> "_IdleAgent":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def chat(self, task: str) -> object:
            await asyncio.Event().wait()
            return None

    def _idle_factory(config: object = None) -> _IdleAgent:
        return _IdleAgent(config)

    mgr = SessionManager(
        approval_waiter=waiter, event_bus=event_bus, agent_factory=_idle_factory
    )
    session_evt = asyncio.Event()

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, session_evt, session_manager=mgr, approval_waiter=waiter),
        path=str(sock_path),
    )

    yield SimpleNamespace(
        sock_path=sock_path,
        state_dir=state_dir,
        workspace=git_workspace,
        waiter=waiter,
        event_bus=event_bus,
        git_diff=git_diff,
        digest_gen=digest_gen,
        publish=publish,
        policy=policy,
        mgr=mgr,
    )

    server.close()
    await server.wait_closed()
    for bg in list(mgr._tasks.values()):
        if not bg.done():
            bg.cancel()
    sock_path.unlink(missing_ok=True)


@pytest.fixture
async def ipc_client(daemon_ctx: SimpleNamespace):
    """Async JSON-RPC client connected to the in-process server."""
    reader, writer = await asyncio.open_unix_connection(str(daemon_ctx.sock_path))
    _id = 0

    async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal _id
        _id += 1
        req = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return json.loads(data)  # type: ignore[no-any-return]

    yield call

    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except (asyncio.TimeoutError, OSError):
        pass


@pytest.fixture
def fake_agent(daemon_ctx: SimpleNamespace):
    """Returns make(session_id, timeout_seconds, task) -> FakeAgent with real hooks."""

    def make(
        session_id: str,
        timeout_seconds: float = 300.0,
        task: str = "",
    ) -> FakeAgent:
        agent = FakeAgent()
        agent.register(
            "session_start",
            CAOOnSessionStartHook(
                session_id=session_id,
                workspace_path=str(daemon_ctx.workspace),
                event_bus=daemon_ctx.publish,
                git_diff_collector=daemon_ctx.git_diff,
                task_description=task,
            ),
        )
        agent.register(
            "pre_tool",
            CAOPreToolCallDecideHook(
                policy_engine=daemon_ctx.policy,
                approval_waiter=daemon_ctx.waiter,
                event_bus=daemon_ctx.publish,
                timeout_seconds=timeout_seconds,
                session_manager=daemon_ctx.mgr,
            ),
        )
        agent.register("post_tool", CAOPostToolCallHook(event_bus=daemon_ctx.publish))
        agent.register(
            "session_end",
            CAOOnSessionEndHook(
                event_bus=daemon_ctx.publish,
                git_diff_collector=daemon_ctx.git_diff,
                digest_generator=daemon_ctx.digest_gen,
                session_manager=daemon_ctx.mgr,
            ),
        )
        return agent

    return make
