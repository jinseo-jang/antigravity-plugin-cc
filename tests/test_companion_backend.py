"""Backend discovery must not depend on CLAUDE_PLUGIN_DATA.

Runtime truth (workstation): Claude Code sets CLAUDE_PLUGIN_{DATA,ROOT} for HOOKS only;
slash commands (and the Bash-tool commands Claude runs from a command's instructions) get
NEITHER as an environment variable — `${CLAUDE_PLUGIN_ROOT}` is markdown-template-substituted,
not exported. So at command time the companion cannot locate the backend via CLAUDE_PLUGIN_DATA.

F1: `_handle_setup` imports `cao.runtime`; it must resolve site-packages from the fixed base
    (`CAO_PLUGIN_DATA or ~/.config/cao`) the SessionStart hook installs to — with both plugin
    env vars unset. A sentinel proves the fixed-base stub was imported (not any globally
    installed cao), so the test is deterministic even on a dev box that has cao installed.
F2: `_autostart_daemon` spawns `python -m cao.runtime.daemon` in a subprocess; a `sys.path`
    insert does NOT propagate, so it must put the same site-packages on the child's PYTHONPATH
    (prepended, preserving any existing value).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_COMPANION_PATH = Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "cao-companion.py"


def _load_companion() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cao_companion_backend", _COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_cao(site_packages: Path, sentinel: Path) -> None:
    runtime = site_packages / "cao" / "runtime"
    runtime.mkdir(parents=True)
    (site_packages / "cao" / "__init__.py").write_text("")
    (runtime / "__init__.py").write_text("")
    (runtime / "compat.py").write_text("def check_model(model):\n    return None\n")
    (runtime / "defaults.py").write_text(
        "import json, os, pathlib\n"
        f"_SENTINEL = {str(sentinel)!r}\n"
        "def store_path():\n"
        "    base = os.environ.get('CAO_PLUGIN_DATA') or os.path.expanduser('~/.config/cao')\n"
        "    return pathlib.Path(base) / 'defaults.json'\n"
        "def save(data):\n"
        "    open(_SENTINEL, 'w').write('used')\n"
        "    p = store_path(); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data))\n"
    )


def test_companion_setup_imports_cao_from_fixed_base_without_plugin_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    base = home / ".config" / "cao"
    sentinel = base / "STUB_CAO_USED"
    base.mkdir(parents=True)
    _stub_cao(base / "site-packages", sentinel)

    # Faithful slash-command env: NO CLAUDE_PLUGIN_DATA, NO CAO_PLUGIN_DATA, NO PYTHONPATH.
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, str(_COMPANION_PATH), "setup", "--mode", "vertex", "--model", "gemini-3.5-flash"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert "cannot import cao.runtime" not in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.exists(), (
        "companion did not import cao from the fixed base ($HOME/.config/cao/site-packages) "
        f"when CLAUDE_PLUGIN_DATA/CAO_PLUGIN_DATA are unset. stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_autostart_daemon_prepends_site_packages_to_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    site_packages = home / ".config" / "cao" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CAO_PLUGIN_DATA", raising=False)  # conftest sets it; hit the ~/.config/cao base
    monkeypatch.setenv("PYTHONPATH", "/preexisting")

    companion = _load_companion()

    captured: dict[str, object] = {}

    class _Boom(Exception):
        pass

    def _fake_popen(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs.get("env")
        raise _Boom()

    monkeypatch.setattr(companion.subprocess, "Popen", _fake_popen)

    with pytest.raises(_Boom):
        companion._autostart_daemon(Path("/tmp/does-not-exist.sock"))

    env = captured["env"]
    assert isinstance(env, dict)
    parts = env.get("PYTHONPATH", "").split(os.pathsep)
    assert parts[0] == str(site_packages), f"site-packages must be PREPENDED; got {env.get('PYTHONPATH')!r}"
    assert "/preexisting" in parts, f"existing PYTHONPATH must be preserved; got {env.get('PYTHONPATH')!r}"


def test_setup_reports_restart_when_backend_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    # Clean slash-command env, no cao installed anywhere under $HOME/.config/cao.
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, str(_COMPANION_PATH), "setup", "--mode", "vertex", "--model", "gemini-3.5-flash"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Restart Claude Code" in result.stdout, result.stdout + result.stderr
    assert "wait a few seconds" not in result.stdout, result.stdout + result.stderr


def test_daemon_path_reports_restart_fast_when_backend_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    # Clean slash-command env, no cao installed anywhere under $HOME/.config/cao.
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, str(_COMPANION_PATH), "session.status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Restart Claude Code" in result.stdout, result.stdout + result.stderr
    assert "daemon did not become ready" not in result.stdout, result.stdout + result.stderr


def test_session_end_stays_silent_when_backend_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    # Clean slash-command env, no cao installed anywhere under $HOME/.config/cao.
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "CAO_NO_AUTOSTART": "1",
    }
    result = subprocess.run(
        [sys.executable, str(_COMPANION_PATH), "session.end"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "", result.stdout + result.stderr
