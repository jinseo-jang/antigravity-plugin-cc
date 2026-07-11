"""SessionStart hook: backend install location + the CLAUDE_ENV_FILE bridge (option 2).

Slash commands don't get CLAUDE_PLUGIN_DATA in their env (Claude Code exports it to hooks only), so
the hook resolves the base as `CAO_PLUGIN_DATA -> CLAUDE_PLUGIN_DATA -> ~/.config/cao`, installs the
backend there, and BRIDGES the resolved dir to commands by appending `export CAO_PLUGIN_DATA=...` to
$CLAUDE_ENV_FILE. #338-safe: only the namespaced CAO_PLUGIN_DATA is bridged — never the reserved
CLAUDE_PLUGIN_DATA and never a global PYTHONPATH.

- without_plugin_env: standalone (no plugin env, no env-file) -> falls back to ~/.config/cao, no bridge.
- in_hook_context: hook has CLAUDE_PLUGIN_DATA + CLAUDE_ENV_FILE -> installs to the standard data dir
  and bridges it via the namespaced var only.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "session_start.sh"


def test_session_start_installs_to_fixed_base_without_plugin_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # pip shim: honor `--target <dir>` by creating an importable cao package there; ignore the rest.
    pip = bin_dir / "pip"
    pip.write_text(
        "#!/usr/bin/env bash\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--target" ]; then shift; mkdir -p "$1/cao"; echo "x" > "$1/cao/__init__.py"; fi\n'
        "  shift\n"
        "done\n"
    )
    pip.chmod(0o755)

    proot = tmp_path / "proot"
    (proot / ".claude-plugin").mkdir(parents=True)
    (proot / ".claude-plugin" / "plugin.json").write_text('{"version": "0.1.0"}')

    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CLAUDE_PLUGIN_ROOT": str(proot),
        # deliberately NO CLAUDE_PLUGIN_DATA and NO CAO_PLUGIN_DATA
    }
    result = subprocess.run(["bash", str(_HOOK)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    base = home / ".config" / "cao"
    assert (base / "site-packages" / "cao" / "__init__.py").exists(), (
        f"hook did not install to $HOME/.config/cao/site-packages. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert (base / ".cao_installed").read_text().strip() == "v0.1.0"


def test_session_start_bridges_cao_plugin_data_in_hook_context(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plugin_data = tmp_path / "cc_plugin_data"  # simulates ~/.claude/plugins/data/{id}
    plugin_data.mkdir()
    env_file = tmp_path / "claude_env_file"
    env_file.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pip = bin_dir / "pip"
    pip.write_text(
        "#!/usr/bin/env bash\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--target" ]; then shift; mkdir -p "$1/cao"; echo "x" > "$1/cao/__init__.py"; fi\n'
        "  shift\n"
        "done\n"
    )
    pip.chmod(0o755)
    proot = tmp_path / "proot"
    (proot / ".claude-plugin").mkdir(parents=True)
    (proot / ".claude-plugin" / "plugin.json").write_text('{"version": "0.1.0"}')

    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CLAUDE_PLUGIN_ROOT": str(proot),
        "CLAUDE_PLUGIN_DATA": str(plugin_data),  # Claude Code gives this to hooks
        "CLAUDE_ENV_FILE": str(env_file),
        # deliberately NO CAO_PLUGIN_DATA
    }
    result = subprocess.run(["bash", str(_HOOK)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    # (a) installed into the STANDARD plugin data dir (CLAUDE_PLUGIN_DATA), not ~/.config/cao
    assert (plugin_data / "site-packages" / "cao" / "__init__.py").exists(), (
        f"hook did not install to CLAUDE_PLUGIN_DATA. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert (plugin_data / ".cao_installed").read_text().strip() == "v0.1.0"
    assert not (home / ".config" / "cao" / "site-packages").exists(), (
        "hook installed to ~/.config/cao even though CLAUDE_PLUGIN_DATA was set"
    )

    # (b) bridged the namespaced var to slash commands via CLAUDE_ENV_FILE
    bridged = env_file.read_text()
    assert "export CAO_PLUGIN_DATA=" in bridged, f"no CAO_PLUGIN_DATA bridge written; env_file={bridged!r}"
    assert str(plugin_data) in bridged, f"bridge points elsewhere; env_file={bridged!r}"

    # (c) #338-safe: never leak the reserved var or a global PYTHONPATH into the session env file
    assert "export CLAUDE_PLUGIN_DATA=" not in bridged, "leaked reserved CLAUDE_PLUGIN_DATA (codex #338)"
    assert "PYTHONPATH" not in bridged, "leaked a global PYTHONPATH into the session env file"
