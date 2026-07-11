from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_split_flags_reference_vector() -> None:
    task, flags = companion._split_flags(
        "--model x --effort high --file a --file b task words"
    )
    assert task == "task words"
    assert flags == {"model": ["x"], "effort": ["high"], "file": ["a", "b"]}


def test_split_flags_boolean_flag() -> None:
    task, flags = companion._split_flags("do it --background")
    assert task == "do it"
    assert flags == {"background": ["true"]}


def test_split_flags_repeated_value_collects_in_order() -> None:
    _, flags = companion._split_flags("--file first --file second --file third")
    assert flags == {"file": ["first", "second", "third"]}


def test_split_flags_quoted_value_with_space() -> None:
    _, flags = companion._split_flags('--file "my report.pdf"')
    assert flags == {"file": ["my report.pdf"]}


def test_parse_params_no_flags_byte_identical() -> None:
    params = companion._parse_params("session.implement", "fix the parser", "/ws")
    assert params == {"task": "fix the parser", "workspace": "/ws"}


def test_parse_params_no_flags_preserves_internal_whitespace() -> None:
    raw = "fix   the    parser"
    params = companion._parse_params("session.implement", raw, "/ws")
    assert params == {"task": raw.strip(), "workspace": "/ws"}


def test_parse_params_marshals_present_flags() -> None:
    params = companion._parse_params(
        "session.implement", "--model gemini-2.5-flash --file a.png make it", "/ws"
    )
    assert params == {
        "task": "make it",
        "workspace": "/ws",
        "model": "gemini-2.5-flash",
        "files": ["a.png"],
    }


def test_build_agent_config_signature_keyword_only() -> None:
    sig = inspect.signature(SessionManager.build_agent_config)
    for name in ("model", "effort", "files", "conversation_id", "system_instructions"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None


def _mgr() -> SessionManager:
    return SessionManager(approval_waiter=ApprovalWaiter())


def test_build_agent_config_noop_equivalence(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project-1")
    ws = str(tmp_path)
    base = _mgr().build_agent_config("sid", ws)
    explicit = _mgr().build_agent_config(
        "sid",
        ws,
        model=None,
        effort=None,
        files=None,
        conversation_id=None,
        system_instructions=None,
    )
    assert base.workspaces == explicit.workspaces
    assert [p.name for p in base.policies] == [p.name for p in explicit.policies]
    assert len(base.hooks) == len(explicit.hooks)
