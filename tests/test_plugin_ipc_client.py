"""Unit tests for CAO companion launcher IPC client.

Tests framing, response rendering, daemon autostart logic, and readiness
timeout. No live daemon needed — sockets and subprocess are mocked.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Load cao-companion.py via importlib (hyphen prevents normal import) ───────
_COMPANION_PATH = (
    Path(__file__).parent.parent / "plugin" / "scripts" / "cao-companion.py"
)
_spec = importlib.util.spec_from_file_location("cao_companion", _COMPANION_PATH)
assert _spec is not None and _spec.loader is not None
_companion = types.ModuleType("cao_companion")
_spec.loader.exec_module(_companion)  # type: ignore[union-attr]
sys.modules["cao_companion"] = _companion  # register so patch() can find it

# Aliases for functions under test
_send_rpc: Any = _companion._send_rpc  # type: ignore[attr-defined]
_render: Any = _companion._render  # type: ignore[attr-defined]
_parse_params: Any = _companion._parse_params  # type: ignore[attr-defined]
_is_daemon_alive: Any = _companion._is_daemon_alive  # type: ignore[attr-defined]
_autostart_daemon: Any = _companion._autostart_daemon  # type: ignore[attr-defined]


# ── Socket mock helper ────────────────────────────────────────────────────────

def _sock_mock(response: dict[str, Any]) -> MagicMock:
    """Return a MagicMock acting as a Unix socket returning *response* as JSONL."""
    data = json.dumps(response).encode() + b"\n"
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.recv.side_effect = [data, b""]
    return m


# ── Framing round-trip ────────────────────────────────────────────────────────

class TestFramingRoundTrip:
    def test_send_produces_newline_delimited_jsonrpc2(self, tmp_path) -> None:
        """Launcher writes a newline-terminated JSON-RPC 2.0 object."""
        mock_sock = _sock_mock({"jsonrpc": "2.0", "id": 1, "result": "pong"})
        with patch("socket.socket", return_value=mock_sock):
            _send_rpc(tmp_path / "test.sock", "ping", {})

        sent: bytes = mock_sock.sendall.call_args[0][0]
        assert sent.endswith(b"\n"), "frame must end with newline"
        msg = json.loads(sent.decode())
        assert msg["jsonrpc"] == "2.0"
        assert "id" in msg
        assert msg["method"] == "ping"
        assert msg["params"] == {}

    def test_ipc_read_message_parses_launcher_output(self, tmp_path) -> None:
        """ipc.read_message (daemon's parser) can consume what the launcher sends."""
        from cao.runtime.ipc import read_message

        mock_sock = _sock_mock({"jsonrpc": "2.0", "id": 1, "result": "pong"})
        with patch("socket.socket", return_value=mock_sock):
            _send_rpc(tmp_path / "test.sock", "ping", {})

        sent: bytes = mock_sock.sendall.call_args[0][0]

        async def _check() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(sent)
            msg = await read_message(reader)
            assert msg["jsonrpc"] == "2.0"
            assert msg["method"] == "ping"

        asyncio.run(_check())


# ── Result rendering ──────────────────────────────────────────────────────────

class TestResultRendering:
    def test_dict_result_with_digest_prints_digest(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Given result dict with 'digest' key, print only the digest string."""
        digest_text = "## Summary\nDid the thing."
        _render({"jsonrpc": "2.0", "id": 1, "result": {"digest": digest_text}})
        out = capsys.readouterr().out.strip()
        assert out == digest_text

    def test_string_result_prints_directly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """String result is printed directly."""
        _render({"jsonrpc": "2.0", "id": 1, "result": "pong"})
        assert capsys.readouterr().out.strip() == "pong"


# ── Error rendering ───────────────────────────────────────────────────────────

class TestErrorRendering:
    def test_busy_error_prints_human_message_not_raw_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Error -32001 prints the human-readable busy message, not raw JSON."""
        _render({"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "busy"}})
        out = capsys.readouterr().out
        assert "already active" in out, "should say session is already active"
        # Must not dump raw JSON
        assert '"code"' not in out
        assert '"error"' not in out

    def test_generic_error_prints_code_and_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-32001 errors print 'Antigravity error <code>: <message>'."""
        _render(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid Request"}}
        )
        out = capsys.readouterr().out
        assert "Antigravity error -32600: Invalid Request" in out


# ── Autostart logic ───────────────────────────────────────────────────────────

class TestAutostartLogic:
    def test_popen_called_with_start_new_session(self, tmp_path) -> None:
        """When daemon is absent, Popen is called with start_new_session=True."""
        mock_popen = MagicMock()
        # _is_daemon_alive: False on first call (before Popen), True after
        alive_seq = [False, True]

        with (
            patch("subprocess.Popen", mock_popen),
            patch.object(_companion, "_is_daemon_alive", side_effect=alive_seq),
            patch.object(_companion.time, "sleep"),
        ):
            _autostart_daemon(tmp_path / "test.sock")

        mock_popen.assert_called_once()
        kwargs = mock_popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True, "must detach with start_new_session=True"
        assert kwargs.get("stdout") is not None  # DEVNULL
        assert kwargs.get("stderr") is not None  # DEVNULL

    def test_polls_for_readiness_after_popen(self, tmp_path) -> None:
        """After Popen, the launcher polls _is_daemon_alive before returning."""
        mock_popen = MagicMock()
        # Three polls before success
        alive_seq = [False, False, False, True]

        with (
            patch("subprocess.Popen", mock_popen),
            patch.object(_companion, "_is_daemon_alive", side_effect=alive_seq),
            patch.object(_companion.time, "sleep") as mock_sleep,
        ):
            _autostart_daemon(tmp_path / "test.sock")

        # sleep should be called between failed polls
        assert mock_sleep.call_count >= 1

    def test_readiness_timeout_exits_code_1(self, capsys: pytest.CaptureFixture[str], tmp_path) -> None:
        """When daemon never becomes ready, print error and exit with code 1."""
        mock_popen = MagicMock()

        # monotonic: deadline=0+10=10, then 11 to blow the loop
        mono_seq = [0.0, 11.0]

        with (
            patch("subprocess.Popen", mock_popen),
            patch.object(_companion, "_is_daemon_alive", return_value=False),
            patch.object(_companion.time, "sleep"),
            patch.object(_companion.time, "monotonic", side_effect=mono_seq),
            pytest.raises(SystemExit) as exc_info,
        ):
            _autostart_daemon(tmp_path / "test.sock")

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "daemon" in out.lower() or "ready" in out.lower()


# ── Param marshaling ──────────────────────────────────────────────────────────

class TestParseParams:
    def test_implement_sets_task_and_workspace(self) -> None:
        p = _parse_params("session.implement", "fix the auth module", "/ws")
        assert p["task"] == "fix the auth module"
        assert p["workspace"] == "/ws"

    def test_approve_requires_call_id(self) -> None:
        with pytest.raises(SystemExit):
            _parse_params("session.approve", "", "/ws")

    def test_approve_default_scope_once(self) -> None:
        p = _parse_params("session.approve", "42", "/ws")
        assert p == {"call_id": "42", "scope": "once"}

    def test_approve_parses_project_scope(self) -> None:
        p = _parse_params("session.approve", "42 project", "/ws")
        assert p == {"call_id": "42", "scope": "project"}

    def test_approve_parses_global_scope(self) -> None:
        p = _parse_params("session.approve", "42 global", "/ws")
        assert p == {"call_id": "42", "scope": "global"}

    def test_approve_ignores_unknown_scope(self) -> None:
        p = _parse_params("session.approve", "42 bogus", "/ws")
        assert p == {"call_id": "42", "scope": "once"}

    def test_deny_splits_call_id_and_reason(self) -> None:
        p = _parse_params("session.deny", "call_001 too risky", "/ws")
        assert p["call_id"] == "call_001"
        assert p["reason"] == "too risky"

    def test_status_empty_args_returns_empty_params(self) -> None:
        assert _parse_params("session.status", "", "/ws") == {}

    def test_events_parses_after_event_id(self) -> None:
        p = _parse_params("session.events", "sess42 7", "/ws")
        assert p["session_id"] == "sess42"
        assert p["after_event_id"] == 7


# ── main() argv marshaling (regression: Claude's Bash tool passes multi-word) ──

class TestMainArgJoining:
    def _run_main(self, argv: list[str]) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_send(sock: Any, method: str, params: dict[str, Any], req_id: int = 1) -> dict[str, Any]:
            captured["method"] = method
            captured["params"] = params
            return {"jsonrpc": "2.0", "id": 1, "result": {"approved": True}}

        with (
            patch.object(sys, "argv", argv),
            patch.object(_companion, "_is_daemon_alive", return_value=True),
            patch.object(_companion, "_send_rpc", side_effect=fake_send),
        ):
            _companion.main()
        return captured

    def test_main_joins_multiword_argv_for_approve_scope(self) -> None:
        # `session.approve 4 project` arrives as separate argv tokens.
        captured = self._run_main(["cao-companion.py", "session.approve", "4", "project"])
        assert captured["method"] == "session.approve"
        assert captured["params"] == {"call_id": "4", "scope": "project"}

    def test_main_joins_multiword_argv_for_deny_reason(self) -> None:
        captured = self._run_main(["cao-companion.py", "session.deny", "4", "too", "risky"])
        assert captured["params"]["call_id"] == "4"
        assert captured["params"]["reason"] == "too risky"

    def test_main_method_only_no_args(self) -> None:
        captured = self._run_main(["cao-companion.py", "session.status"])
        assert captured["method"] == "session.status"
        assert captured["params"] == {}
