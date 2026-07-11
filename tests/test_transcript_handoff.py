from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from cao.runtime import transcript
from cao.runtime.approval_waiter import ApprovalWaiter
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


# --- AC1: parser extracts user/assistant turns -----------------------------


def test_read_turns_extracts_user_and_assistant(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "hello there"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "line one"},
                        {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
                        {"type": "text", "text": "line two"},
                    ],
                },
            }
        ),
        json.dumps({"type": "summary", "summary": "ignore me"}),
        json.dumps({"type": "system", "content": "ignore me too"}),
        "this is not json {{{",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    turns = transcript.read_turns(path)

    assert len(turns) == 2
    assert turns[0].role == "user"
    assert turns[0].text == "hello there"
    assert turns[1].role == "assistant"
    assert turns[1].text == "line one\nline two"  # tool_use dropped


def test_read_turns_missing_file_returns_empty() -> None:
    assert transcript.read_turns("/no/such/file.jsonl") == []


def test_read_turns_drops_empty_text_turns(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "bash", "input": {}}],
                },
            }
        ),
        json.dumps({"type": "user", "message": {"role": "user", "content": "kept"}}),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    turns = transcript.read_turns(path)
    assert [t.text for t in turns] == ["kept"]


# --- AC2: summary respects char/turn caps ----------------------------------


def test_summarize_respects_caps() -> None:
    turns = [
        transcript.TranscriptTurn(role="user", text=f"turn-{i} " + "x" * 500)
        for i in range(100)
    ]
    # unique markers: earliest turn absent, most recent present
    turns[0] = transcript.TranscriptTurn(role="user", text="EARLYUNIQUE " + "x" * 500)
    turns[-1] = transcript.TranscriptTurn(role="user", text="RECENTUNIQUE " + "x" * 500)

    out = transcript.summarize(turns)

    assert len(out) <= transcript._MAX_CHARS + len(transcript._EARLIER_MARKER)
    assert "EARLYUNIQUE" not in out
    assert "RECENTUNIQUE" in out


def test_summarize_truncates_long_single_turn() -> None:
    long_turn = transcript.TranscriptTurn(role="user", text="y" * 5000)
    out = transcript.summarize([long_turn])
    assert "…[truncated]" in out
    assert len(out) <= transcript._MAX_TURN_CHARS + len("USER: ") + len(" …[truncated]")


def test_summarize_empty_returns_empty() -> None:
    assert transcript.summarize([]) == ""


def test_load_handoff_summary_none_and_missing() -> None:
    assert transcript.load_handoff_summary(None) is None
    assert transcript.load_handoff_summary("/no/such/file") is None


# --- AC3: system_instructions composition (base + handoff, no clobber) ------


def test_build_handoff_instructions_summary_only() -> None:
    out = transcript.build_handoff_instructions("SUMMARY")
    assert transcript.HANDOFF_PREAMBLE in out
    assert "SUMMARY" in out
    assert out.index(transcript.HANDOFF_PREAMBLE) < out.index("SUMMARY")


def test_build_handoff_instructions_with_base_no_clobber() -> None:
    out = transcript.build_handoff_instructions("SUMMARY", base="BASE")
    assert out.index("BASE") < out.index(transcript.HANDOFF_PREAMBLE) < out.index("SUMMARY")


def test_build_agent_config_composes_system_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    mgr = SessionManager(approval_waiter=ApprovalWaiter())

    cfg = mgr.build_agent_config("s1", "/ws", system_instructions="SUMMARY")
    assert transcript.HANDOFF_PREAMBLE in cfg.system_instructions
    assert "SUMMARY" in cfg.system_instructions

    plain = mgr.build_agent_config("s2", "/ws")
    assert plain.system_instructions is None


# --- AC4: launcher marshals target + transcript path ------------------------


def test_parse_params_handoff_with_transcript_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_TRANSCRIPT_PATH", str(tmp_path / "abc.jsonl"))
    params = companion._parse_params("session.handoff", "finish the refactor", "/ws")
    assert params == {
        "target": "finish the refactor",
        "workspace": "/ws",
        "transcript_path": str(tmp_path / "abc.jsonl"),
    }


def test_parse_params_handoff_without_transcript_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_TRANSCRIPT_PATH", raising=False)
    params = companion._parse_params("session.handoff", "finish the refactor", "/ws")
    assert params == {"target": "finish the refactor", "workspace": "/ws"}
    assert "transcript_path" not in params
