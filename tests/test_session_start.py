"""SessionStart hook must install the backend to the fixed base, not CLAUDE_PLUGIN_DATA.

The companion (slash commands) cannot see CLAUDE_PLUGIN_DATA, so it resolves the backend from
`CAO_PLUGIN_DATA or ~/.config/cao`. The hook MUST install to the same base, or the hook and the
companion diverge (hook installs where the companion never looks). This runs the real hook with a
`pip` shim and BOTH plugin env vars unset, asserting it installs to `$HOME/.config/cao`.
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
