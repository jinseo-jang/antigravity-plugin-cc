"""Task 007 — model & effort selection unit tests (AC1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from google.antigravity.models import (  # type: ignore[import-untyped]
    GeminiAPIEndpoint,
    ModelTarget,
    ThinkingLevel,
    VertexEndpoint,
)

from cao.models import DiffSummary, RuntimeEvent
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.auth import AuthConfig, to_local_agent_kwargs
from cao.runtime.daemon import handle_client
from cao.runtime.digest_generator import _build_markdown
from cao.runtime.ipc import read_message, write_message
from cao.runtime.session_manager import SessionManager


def _vertex_auth() -> AuthConfig:
    return AuthConfig(
        mode="vertex", model="gemini-2.5-flash", project="p", location="us-central1", api_key=None
    )


def _api_key_auth() -> AuthConfig:
    return AuthConfig(
        mode="gemini_api_key", model="gemini-2.5-flash", project=None, location=None, api_key="k"
    )


def test_effort_puts_thinking_level_on_vertex_endpoint() -> None:
    mt = to_local_agent_kwargs(_vertex_auth(), effort="high")["model"]
    assert isinstance(mt, ModelTarget)
    assert isinstance(mt.endpoint, VertexEndpoint)
    assert mt.endpoint.options.thinking_level == ThinkingLevel.HIGH
    # R2 trap: ModelTarget itself must NOT carry thinking_level.
    assert getattr(mt, "thinking_level", None) is None


def test_effort_puts_thinking_level_on_api_key_endpoint() -> None:
    mt = to_local_agent_kwargs(_api_key_auth(), effort="minimal")["model"]
    assert isinstance(mt, ModelTarget)
    assert isinstance(mt.endpoint, GeminiAPIEndpoint)
    assert mt.endpoint.options.thinking_level == ThinkingLevel.MINIMAL
    assert getattr(mt, "thinking_level", None) is None


def test_cli_model_overrides_cao_model() -> None:
    auth = AuthConfig(
        mode="vertex", model="gemini-2.0-flash", project="p", location="l", api_key=None
    )
    assert to_local_agent_kwargs(auth, model="gemini-3.5-flash")["model"] == "gemini-3.5-flash"


def test_no_flag_parity_is_plain_string() -> None:
    auth = _vertex_auth()
    kwargs = to_local_agent_kwargs(auth)
    assert kwargs["model"] == auth.model
    assert not isinstance(kwargs["model"], ModelTarget)
    assert kwargs == {"model": "gemini-2.5-flash", "vertex": True, "project": "p", "location": "us-central1"}


async def test_invalid_effort_rejected_pre_start(tmp_path: Path) -> None:
    sock = tmp_path / "eff.sock"
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
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
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.implement",
                "params": {"task": "do anything", "workspace": str(tmp_path), "effort": "turbo"},
            },
        )
        resp = await read_message(r)
        w.close()
        await w.wait_closed()
    assert resp["error"]["code"] == -32602
    assert "turbo" in resp["error"]["message"]
    assert "minimal, low, medium, high" in resp["error"]["message"]
    # No session created for a bad effort.
    assert mgr.get_active_session_id("default") is None


def _ev(session_id: str, event_type: str, payload: dict[str, object]) -> RuntimeEvent:
    return RuntimeEvent(
        id=1,
        session_id=session_id,
        type=event_type,
        timestamp_utc=datetime.now(timezone.utc),
        payload=payload,
    )


def test_digest_model_line_present() -> None:
    events = [
        _ev("s1", "session.started", {"task": "x"}),
        _ev("s1", "session.model", {"model": "gemini-3.5-flash", "effort": "high"}),
    ]
    md = _build_markdown("s1", events, DiffSummary())
    assert "**Model / effort:**" in md
    assert "`gemini-3.5-flash`" in md
    assert "/ high" in md


def test_digest_model_line_absent_without_event() -> None:
    md = _build_markdown("s1", [_ev("s1", "session.started", {"task": "x"})], DiffSummary())
    assert "**Model / effort:**" not in md
