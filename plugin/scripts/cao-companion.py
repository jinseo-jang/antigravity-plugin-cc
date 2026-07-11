#!/usr/bin/env python3
"""CAO companion launcher: parse args → marshal params → send JSON-RPC → print result.

Invocation:
    python cao-companion.py <method> <raw-args-string>

Zero business logic. Dumb pipe: socket path, autostart, forward, render.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import time
from typing import Any, cast

# The SessionStart hook installs the cao backend into "<base>/site-packages". Resolve <base> the
# SAME way the hook and src/cao/* do — CAO_PLUGIN_DATA else ~/.config/cao — and NEVER via
# CLAUDE_PLUGIN_DATA: Claude Code exports CLAUDE_PLUGIN_DATA to hooks only, not to slash commands, so
# relying on it here left the backend unfindable at command time. Put site-packages on sys.path (our
# own imports) and, in _autostart_daemon, on the daemon subprocess's PYTHONPATH (a sys.path insert
# does not propagate to a child process).
_CAO_BASE = os.environ.get("CAO_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".config", "cao")
_SITE_PACKAGES = os.path.join(_CAO_BASE, "site-packages")
if os.path.isdir(_SITE_PACKAGES) and _SITE_PACKAGES not in sys.path:
    sys.path.insert(0, _SITE_PACKAGES)


_POLL_INTERVAL: float = 0.2
_AUTOSTART_TIMEOUT: float = 10.0


# ponytail: mirrors cao.runtime.workspace EXACTLY (companion cannot import cao);
# tests/test_companion_socket.py cross-checks the two copies never diverge.
_MARKERS = (".git", ".claude-plugin")


def _state_dir(workspace: Path) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", workspace.name)
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    root = (
        Path(env_data) / "state"
        if env_data
        else Path(tempfile.gettempdir()) / "cao-companion"
    )
    return root / f"{slug}-{digest}"


def _resolve_workspace() -> Path:
    # Byte-behavioral mirror of cao.runtime.workspace._find_root (companion can't
    # import cao); tests/test_companion_socket.py cross-checks they never diverge.
    env = os.environ.get("CAO_WORKSPACE")
    if env:
        return Path(env).resolve()
    blocked = {Path("/"), Path("/tmp"), Path(tempfile.gettempdir()).resolve()}
    for _env in ("HOME", "TMPDIR"):
        _val = os.environ.get(_env)
        if _val:
            blocked.add(Path(_val).resolve())
    start = Path.cwd().resolve()
    for d in [start, *start.parents]:  # marker walk; cwd exempt, parents blocklisted
        if d != start and d in blocked:
            continue
        if any((d / m).exists() for m in _MARKERS):
            return d
    for d in start.parents:  # strict ancestors bearing a deliberate root marker
        if d in blocked:
            continue
        if (_state_dir(d) / "root").exists():
            return d
    return start


def _socket_path() -> Path:
    return _state_dir(_resolve_workspace()) / "rpc.sock"


def _send_rpc(
    sock_path: Path,
    method: str,
    params: dict[str, Any],
    req_id: int = 1,
) -> dict[str, Any]:
    """Open a fresh connection, send one JSON-RPC 2.0 request, return response dict."""
    request: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    raw: Any = json.loads(buf.split(b"\n")[0])
    if not isinstance(raw, dict):
        raise ValueError(f"Unexpected response type: {type(raw)}")
    return cast(dict[str, Any], raw)


def _is_daemon_alive(sock_path: Path) -> bool:
    """True if daemon responds to ping."""
    try:
        resp = _send_rpc(sock_path, "ping", {})
        return resp.get("result") == "pong"
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _autostart_daemon(sock_path: Path) -> None:
    """Spawn daemon detached; ping-poll until ready or timeout → exit 1."""
    # The daemon runs in a NEW process, so our sys.path insert does not reach it; hand the backend to
    # it on PYTHONPATH (prepended, preserving any existing value) so `-m cao.runtime.daemon` resolves.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SITE_PACKAGES, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [_SITE_PACKAGES]
    )
    subprocess.Popen(
        [sys.executable, "-m", "cao.runtime.daemon"],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + _AUTOSTART_TIMEOUT
    while time.monotonic() < deadline:
        if _is_daemon_alive(sock_path):
            return
        time.sleep(_POLL_INTERVAL)
    print(
        "Antigravity: daemon did not become ready within 10 seconds. "
        "Check your environment or try again.",
        flush=True,
    )
    sys.exit(1)


def _split_flags(
    raw: str,
    bool_flags: frozenset[str] = frozenset({"background", "resume", "fresh"}),
) -> tuple[str, dict[str, list[str]]]:
    """Split a raw CLI arg string into (free_text_task, flags).

    Extracts `--key value` (value-taking) and boolean `--flag` tokens.
    Repeatable flags (e.g. `--file`) collect into a list. Everything that is
    not a flag or a flag value is the free-text task, joined by single spaces.
    Pure transport marshaling — no policy, no semantics.
    """
    # Backward-compat fast path (load-bearing): no `--` token → byte-identical
    # to today's raw.strip(); never enters the tokenizer.
    # ponytail: a literal free-text token beginning with `--` is treated as a
    # flag (rare; quote it to keep it literal — upgrade path is a `--`
    # end-of-flags sentinel if ever needed).
    if not any(tok.startswith("--") for tok in raw.split()):
        return raw.strip(), {}

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()  # unbalanced quotes → best-effort split

    free: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            if key in bool_flags:
                flags.setdefault(key, []).append("true")
            elif i + 1 < len(tokens):
                flags.setdefault(key, []).append(tokens[i + 1])
                i += 1
            else:
                flags.setdefault(key, []).append("true")  # trailing bare flag
        else:
            free.append(tok)
        i += 1
    return " ".join(free), flags


def _apply_shared_flags(params: dict[str, Any], flags: dict[str, list[str]]) -> None:
    """Place model/effort/files flags into JSON-RPC params. Last value wins for scalars."""
    if flags.get("model"):
        params["model"] = flags["model"][-1]  # ponytail: CLI last-wins
    if flags.get("effort"):
        params["effort"] = flags["effort"][-1]
    if flags.get("file"):
        params["files"] = flags["file"]  # repeatable → list, order preserved


def _parse_params(method: str, raw: str, workspace: str) -> dict[str, Any]:
    """Marshal CLI arg string → structured JSON-RPC params.

    Pure transport marshaling — no policy, no git, no SDK calls.
    """
    args = raw.strip()

    if method == "session.implement":
        # resume is value-taking (`--resume <id>` targets a run; bare -> latest).
        task_text, flags = _split_flags(raw, bool_flags=frozenset({"background", "fresh"}))
        impl_params: dict[str, Any] = {"task": task_text, "workspace": workspace}
        _apply_shared_flags(impl_params, flags)
        resume = "resume" in flags
        resume_val = flags["resume"][-1] if resume else "true"
        if resume_val.startswith("--"):
            # ponytail: recovery for `--resume --fresh` where _split_flags eats the
            # next token as --resume's value. Restore it as a bare bool flag.
            flags.setdefault(resume_val[2:], []).append("true")
            resume_val = "true"
        fresh = bool(flags.get("fresh"))
        if flags.get("background"):
            impl_params["background"] = True
        if fresh and resume:
            print(
                "Antigravity: --fresh overrides --resume; starting a fresh conversation.",
                flush=True,
            )
            resume = False
        if fresh:
            impl_params["resume"] = False
        elif resume:
            impl_params["resume"] = True
            if resume_val != "true":
                impl_params["conversation_id"] = resume_val
        return impl_params

    if method == "session.review":
        return {"target": args, "workspace": workspace}

    if method == "session.handoff":
        target_text, flags = _split_flags(raw, bool_flags=frozenset({"background"}))
        handoff_params: dict[str, Any] = {"target": target_text, "workspace": workspace}
        if flags.get("background"):
            handoff_params["background"] = True
        transcript_path = os.environ.get("CLAUDE_TRANSCRIPT_PATH")
        if transcript_path:
            handoff_params["transcript_path"] = transcript_path
        return handoff_params

    if method == "session.status":
        return {"session_id": args} if args else {}

    if method == "session.wait":
        return {"session_id": args} if args else {}

    if method == "session.events":
        tokens = args.split(None, 1)
        params: dict[str, Any] = {}
        if tokens:
            params["session_id"] = tokens[0]
        if len(tokens) > 1:
            try:
                params["after_event_id"] = int(tokens[1])
            except ValueError:
                pass  # omit if not a valid int
        return params

    if method == "session.approve":
        tokens = args.split(None, 1)
        if not tokens or not tokens[0]:
            print("Antigravity error: call_id is required for /agy:approve", flush=True)
            sys.exit(1)
        approve: dict[str, Any] = {"call_id": tokens[0], "scope": "once"}
        if len(tokens) > 1 and tokens[1].strip() in ("project", "global"):
            approve["scope"] = tokens[1].strip()
        return approve

    if method == "session.deny":
        tokens = args.split(None, 1)
        if not tokens or not tokens[0]:
            print("Antigravity error: call_id is required for /agy:deny", flush=True)
            sys.exit(1)
        result: dict[str, Any] = {"call_id": tokens[0]}
        if len(tokens) > 1 and tokens[1]:
            result["reason"] = tokens[1]
        return result

    if method == "session.retry":
        return {"strategy": args} if args else {}

    if method == "session.cancel":
        return {"session_id": args} if args else {}

    # Unknown method: forward with empty params; daemon returns method-not-found
    return {}


def _print_pending(result: dict[str, Any]) -> None:
    """Print pending approvals as ready-to-paste /agy:approve and /agy:deny lines."""
    state = result.get("state")
    if state:
        print(f"State: {state}")
    pending: Any = result.get("pending_approvals") or []
    if not pending:
        print("No pending approvals.")
        return
    print("Pending approval(s) - paste one line to respond:")
    for p in pending:
        call_id = p.get("call_id", "?")
        command = p.get("command", "")
        print(f"  - command: {command}")
        print(f"    approve once:    /agy:approve {call_id}")
        print(f"    approve project: /agy:approve {call_id} project")
        print(f"    approve always:  /agy:approve {call_id} global")
        print(f"    deny:            /agy:deny {call_id}")


def _handle_setup(argv: list[str]) -> None:
    """Write model/region defaults locally — pure local, no daemon needed."""
    flags: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--") and i + 1 < len(argv):
            flags[tok[2:]] = argv[i + 1]
            i += 2
        else:
            i += 1

    # ponytail: only known keys; api-key intentionally excluded
    data: dict[str, str] = {k: flags[k] for k in ("model", "location", "mode", "project") if k in flags}

    # Default location to global when the user didn't pick one — it works for both
    # models. Any region the user chooses is kept as-is (Gemini regions keep growing).
    if data.get("mode", "vertex") == "vertex" and "location" not in data:
        data["location"] = "global"

    try:
        from cao.runtime import compat, defaults
    except ImportError as exc:
        print(
            f"Antigravity: backend not installed yet ({exc}). The SessionStart hook installs it into "
            f"{_SITE_PACKAGES}; wait a few seconds and re-run, or check {_CAO_BASE}/.cao_install.log.",
            flush=True,
        )
        sys.exit(1)

    msg = compat.check_model(data.get("model"))
    if msg:
        print(f"Antigravity setup rejected: {msg}", flush=True)
        sys.exit(1)

    defaults.save(data)
    print(f"Antigravity: defaults saved → {defaults.store_path()}", flush=True)

    # ponytail: soft-warn only — the key is legitimately exported after setup, before implement
    if data.get("mode") == "gemini_api_key":
        base = os.environ.get("CAO_PLUGIN_DATA") or str(Path.home() / ".config" / "cao")
        key_file = Path(base) / "gemini_api_key"
        has_env = bool(os.environ.get("GEMINI_API_KEY"))
        has_file = key_file.is_file()
        # ponytail: probe keychain inline, not via auth._read_keychain — auth.py imports the
        # SDK and setup must stay SDK-free. ~2min stall ceiling if a keyring backend is broken;
        # setup is rare/interactive so the naive probe is fine.
        try:
            import keyring

            has_keychain = bool(keyring.get_password("cao", "gemini_api_key"))
        except Exception:  # noqa: BLE001 — keyring absent or backend broken = no keychain key
            has_keychain = False

        if not has_keychain and not has_env and not has_file:
            print(
                "Antigravity: ⚠ No Gemini key found. The key is never stored by setup. "
                "Provide it one of three ways (checked in this order): "
                "(1) OS keychain — `python -m keyring set cao gemini_api_key` (encrypted at rest, recommended); "
                "(2) export GEMINI_API_KEY in the shell that launches Claude Code, then RESTART it "
                "(a running session and its daemon won't see a later export); "
                "(3) write it to ~/.config/cao/gemini_api_key (chmod 600) — last resort, NOT encrypted.",
                flush=True,
            )
        elif has_file:
            try:
                if os.stat(key_file).st_mode & 0o077:
                    print(
                        "Antigravity: ⚠ Key file is group/world-readable. "
                        "Run `chmod 600 ~/.config/cao/gemini_api_key` to secure it.",
                        flush=True,
                    )
            except OSError:
                pass


def _render(response: dict[str, Any]) -> None:
    """Print human-readable output. Never raw JSON, never tracebacks."""
    if "result" in response:
        result: Any = response["result"]
        if isinstance(result, dict):
            kind: Any = result.get("kind")
            digest: Any = result.get("digest")
            if digest is not None:
                print(digest)
            elif kind == "approval" or result.get("pending_approvals"):
                _print_pending(result)
            elif kind == "done":
                print(f"Session finished (state: {result.get('state', 'done')}).")
            elif kind == "running":
                print(
                    f"Session running (state: {result.get('state', 'running')}). "
                    "Run /agy:watch again to keep watching."
                )
            elif "state" in result:
                print(f"State: {result['state']}")
            else:
                print(json.dumps(result, indent=2))
        else:
            print(result)
    elif "error" in response:
        err: Any = response["error"]
        if isinstance(err, dict):
            code = err.get("code", 0)
            msg = err.get("message", "unknown error")
            if code == -32001:
                print(
                    "Antigravity: A session is already active. "
                    "Use /agy:status to check it, or /agy:cancel to stop it."
                )
            else:
                print(f"Antigravity error {code}: {msg}")
        else:
            print(f"Antigravity error: {err}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: cao-companion.py <method> [args...]", file=sys.stderr)
        sys.exit(1)

    method = sys.argv[1]

    if method == "setup":
        _handle_setup(sys.argv[2:])
        return

    # Join all trailing tokens: Claude's Bash tool passes multi-word args as
    # separate argv (e.g. `session.approve 4 project`), not one quoted string.
    raw_args = " ".join(sys.argv[2:])
    workspace = str(_resolve_workspace())
    sock = _socket_path()

    # CAO_NO_AUTOSTART=1: skip autostart (used by SessionEnd hook — skip silently if down)
    no_autostart = os.environ.get("CAO_NO_AUTOSTART") == "1"

    if not _is_daemon_alive(sock):
        if no_autostart:
            sys.exit(0)
        _autostart_daemon(sock)

    try:
        params = _parse_params(method, raw_args, workspace)
        response = _send_rpc(sock, method, params)
        _render(response)
    except OSError as exc:
        print(f"Antigravity: connection error: {exc}", flush=True)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Antigravity: invalid response from daemon: {exc}", flush=True)
        sys.exit(1)
    except ValueError as exc:
        print(f"Antigravity: protocol error: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
