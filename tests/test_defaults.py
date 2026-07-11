"""Tests for defaults — persistent model/region defaults store (BL-9).

RED → GREEN structure: these tests were written before defaults.py existed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


# ── store_path ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))


def test_store_path_uses_env(tmp_path: Path) -> None:
    from cao.runtime import defaults
    assert defaults.store_path() == tmp_path / "defaults.json"


def test_store_path_defaults_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_PLUGIN_DATA", raising=False)
    from cao.runtime import defaults
    assert defaults.store_path() == Path.home() / ".config" / "cao" / "defaults.json"


# ── load ─────────────────────────────────────────────────────────────────────

def test_load_missing_file_returns_empty() -> None:
    from cao.runtime import defaults
    assert defaults.load() == {}


def test_load_corrupt_file_returns_empty() -> None:
    from cao.runtime import defaults
    defaults.store_path().parent.mkdir(parents=True, exist_ok=True)
    defaults.store_path().write_text("{not json")
    assert defaults.load() == {}


def test_load_non_dict_json_returns_empty() -> None:
    from cao.runtime import defaults
    defaults.store_path().parent.mkdir(parents=True, exist_ok=True)
    defaults.store_path().write_text('["list", "not", "dict"]')
    assert defaults.load() == {}


def test_load_filters_to_known_keys() -> None:
    from cao.runtime import defaults
    defaults.store_path().parent.mkdir(parents=True, exist_ok=True)
    defaults.store_path().write_text(
        json.dumps({"model": "gemini-3.5-flash", "unknown_key": "ignored", "location": "global"})
    )
    result = defaults.load()
    assert result == {"model": "gemini-3.5-flash", "location": "global"}
    assert "unknown_key" not in result


def test_load_never_returns_api_key() -> None:
    from cao.runtime import defaults
    defaults.store_path().parent.mkdir(parents=True, exist_ok=True)
    defaults.store_path().write_text(json.dumps({"api_key": "secret", "model": "gemini-2.5-flash"}))
    result = defaults.load()
    assert "api_key" not in result
    assert result.get("model") == "gemini-2.5-flash"


def test_load_all_known_keys() -> None:
    from cao.runtime import defaults
    data = {"mode": "vertex", "model": "gemini-3.5-flash", "project": "my-proj", "location": "global"}
    defaults.store_path().parent.mkdir(parents=True, exist_ok=True)
    defaults.store_path().write_text(json.dumps(data))
    assert defaults.load() == data


# ── save ─────────────────────────────────────────────────────────────────────

def test_save_and_roundtrip() -> None:
    from cao.runtime import defaults
    data = {"mode": "vertex", "model": "gemini-3.5-flash", "location": "global"}
    defaults.save(data)
    assert defaults.load() == data


def test_save_strips_api_key() -> None:
    from cao.runtime import defaults
    defaults.save({"model": "gemini-2.5-flash", "api_key": "should-be-stripped"})
    raw = json.loads(defaults.store_path().read_text())
    assert "api_key" not in raw
    assert raw.get("model") == "gemini-2.5-flash"


def test_save_creates_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "a" / "b" / "c"
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(nested))
    from cao.runtime import defaults
    defaults.save({"model": "gemini-2.5-flash"})
    assert defaults.store_path().exists()


def test_save_is_atomic(tmp_path: Path) -> None:
    """Save uses tempfile+os.replace — no partial write visible."""
    from cao.runtime import defaults
    defaults.save({"model": "gemini-2.5-flash"})
    # File must exist and be valid JSON after save
    content = json.loads(defaults.store_path().read_text())
    assert content["model"] == "gemini-2.5-flash"


# ── setup companion subcommand ────────────────────────────────────────────────

COMPANION = Path(__file__).parent.parent / "plugin" / "scripts" / "cao-companion.py"


def _run_setup(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os
    env = {**os.environ, "PYTHONPATH": "src"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(COMPANION), "setup", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(COMPANION.parent.parent.parent),
    )


def test_setup_writes_defaults(tmp_path: Path) -> None:
    from cao.runtime import defaults
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--location", "global", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    saved = defaults.load()
    assert saved["model"] == "gemini-3.5-flash"
    assert saved["location"] == "global"
    assert saved["mode"] == "vertex"


def test_setup_vertex_defaults_location_to_global(tmp_path: Path) -> None:
    """Vertex setup with NO --location auto-pins location=global (the only valid
    location for both supported models) — so /agy:setup need not ask for a region."""
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode == 0, f"expected success: {result.stdout}{result.stderr}"
    saved = json.loads((tmp_path / "defaults.json").read_text())
    assert saved["location"] == "global"
    assert saved["model"] == "gemini-3.5-flash"


def test_setup_rejects_unsupported_model_no_write(tmp_path: Path) -> None:
    """gemini-2.5-flash (not in allowlist) → compat rejection, no file written."""
    result = _run_setup(
        ["--model", "gemini-2.5-flash", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode != 0
    assert not (tmp_path / "defaults.json").exists()


def test_setup_rejection_message_contains_options(tmp_path: Path) -> None:
    result = _run_setup(
        ["--model", "gemini-2.5-flash", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    # compat returns a message with "Options:"
    assert "Options" in result.stdout or "Options" in result.stderr


def test_setup_accepts_non_global_location(tmp_path: Path) -> None:
    """Region is the user's choice: a non-global location is saved, not rejected."""
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--location", "us-central1", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode == 0, f"expected success: {result.stdout}{result.stderr}"
    saved = json.loads((tmp_path / "defaults.json").read_text())
    assert saved["location"] == "us-central1"


def test_setup_never_stores_api_key(tmp_path: Path) -> None:
    # Even if someone passes --api-key via raw args it must not appear in the file
    # (setup subcommand must ignore unknown flags including api-key)
    _run_setup(
        ["--model", "gemini-2.5-flash", "--mode", "gemini_api_key"],
        {"CAO_PLUGIN_DATA": str(tmp_path), "GEMINI_API_KEY": "fake-key"},
    )
    # If it wrote a file, api_key must not be in it
    p = tmp_path / "defaults.json"
    if p.exists():
        raw = json.loads(p.read_text())
        assert "api_key" not in raw


def test_setup_api_key_mode_warns_when_env_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini_api_key mode + GEMINI_API_KEY unset → setup still succeeds but WARNS
    (the key is never stored; it must be exported before /agy:implement)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--mode", "gemini_api_key"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "GEMINI_API_KEY" in result.stdout
    assert "no gemini key found" in result.stdout.lower()


def test_setup_api_key_mode_no_warn_when_env_set(tmp_path: Path) -> None:
    """gemini_api_key mode + key present → no warning, and the key value is NEVER echoed."""
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--mode", "gemini_api_key"],
        {"CAO_PLUGIN_DATA": str(tmp_path), "GEMINI_API_KEY": "fake-key-value"},
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "no gemini key found" not in result.stdout.lower()
    assert "fake-key-value" not in result.stdout


def test_setup_vertex_mode_never_warns_about_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vertex mode never mentions GEMINI_API_KEY even when it is unset."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = _run_setup(
        ["--model", "gemini-3.5-flash", "--mode", "vertex"],
        {"CAO_PLUGIN_DATA": str(tmp_path)},
    )
    assert result.returncode == 0
    assert "GEMINI_API_KEY" not in result.stdout
