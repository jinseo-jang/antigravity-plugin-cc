"""asyncio Unix socket server with JSON-RPC 2.0 dispatch and broker pattern.

Entry point::

    CAO_WORKSPACE=$(pwd) python -m cao.runtime.daemon

Socket path is derived from the workspace slug-hash scheme defined in
``event_bus_and_persistence.md §7``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
from pathlib import Path
from typing import Any, cast

from cao.models import ApprovalDecision
from cao.runtime import approval_store, workspace as ws
from cao.runtime.approval_waiter import ApprovalWaiter, UnknownCallIdError
from cao.runtime.auth import AuthNotConfigured, resolve_auth
from cao.runtime.compat import check_model
from cao.runtime.ipc import write_message
from cao.runtime.probe import check_region_available
from cao.runtime.multimodal import AttachmentError, resolve_attachments
from cao.runtime.session_manager import SessionAlreadyActiveError, SessionManager
from cao.runtime import session_store, transcript

logger = logging.getLogger("cao.daemon")

# ponytail: 60 s idle timeout; lower to ~5 s if DoS from idle connections matters
CLIENT_READ_TIMEOUT: float = 60.0


# ---------------------------------------------------------------------------
# Socket path (slug-hash scheme)
# ---------------------------------------------------------------------------


def compute_state_dir(workspace: Path) -> Path:
    """Return the workspace-isolated state directory (slug-hash scheme, §7), created."""
    state_dir = ws.state_dir(workspace)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return state_dir


def socket_path() -> Path:
    """Compute the Unix socket path, anchored to the project root (BL-21)."""
    return compute_state_dir(ws.resolve_workspace()) / "rpc.sock"


def _ping(sock_path: Path, timeout: float = 0.5) -> bool:
    """True iff a live daemon already answers ping on *sock_path*.

    Short synchronous probe used inside serve()'s startup lock to tell a live
    daemon from a stale socket. Any connect/timeout/decode failure means "no live
    daemon" (mirrors the companion's ``_is_daemon_alive``).
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(sock_path))
            client.sendall(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
        raw: Any = json.loads(buf.split(b"\n")[0])
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and bool(raw.get("result") == "pong")


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _effort_error(effort: str) -> str | None:
    """Return a -32602 message if *effort* is a non-empty invalid level, else None."""
    if not effort:
        return None
    from google.antigravity.models import ThinkingLevel  # type: ignore[import-untyped]

    allowed = [e.value for e in ThinkingLevel]
    if effort in allowed:
        return None
    return f"Invalid effort '{effort}'. Expected one of: {', '.join(allowed)}."


# ---------------------------------------------------------------------------
# Client handler
# ---------------------------------------------------------------------------


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session_event: asyncio.Event,
    *,
    session_manager: SessionManager | None = None,
    approval_waiter: ApprovalWaiter | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Serve one connected client until EOF, idle timeout, or error.

    Backward-compatible signature: existing tests pass only session_event.
    When session_manager + approval_waiter are provided, real dispatch is used.
    ``shutdown_event`` (when provided) is set by ``session.shutdown`` to stop the
    whole daemon, not just the current session.
    """
    owns_event = False
    try:
        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=CLIENT_READ_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("client idle timeout; closing connection")
                break

            if not line:
                break

            try:
                raw: Any = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("parse error on input %.80r", line)
                await write_message(writer, _err(None, -32700, "Parse error"))
                continue

            if not isinstance(raw, dict):
                await write_message(writer, _err(None, -32600, "Invalid Request"))
                continue

            msg = cast(dict[str, Any], raw)

            if msg.get("jsonrpc") != "2.0":
                await write_message(writer, _err(msg.get("id"), -32600, "Invalid Request"))
                continue

            msg_id: Any = msg.get("id")
            method_val: Any = msg.get("method")

            if not isinstance(msg_id, (int, str)) or not isinstance(method_val, str):
                await write_message(writer, _err(msg_id, -32600, "Invalid Request"))
                continue

            method: str = method_val
            params: Any = msg.get("params") or {}
            params_dict: dict[str, Any] = params if isinstance(params, dict) else {}

            # ------------------------------------------------------------------
            # Dispatch
            # ------------------------------------------------------------------

            if method == "ping":
                await write_message(writer, _ok(msg_id, "pong"))

            elif method == "session.shutdown":
                # SessionEnd signal: cancel any in-flight session before acking so
                # a shutdown stops work instead of orphaning a running turn.
                if session_manager is not None:
                    active_sd = session_manager.active_session_id()
                    if active_sd is not None:
                        await session_manager.cancel_session(active_sd)
                await write_message(writer, _ok(msg_id, {"status": "ok"}))
                # Stop the daemon itself (ack drained above first) so the next Claude
                # Code session spawns a fresh one — otherwise the process outlives the
                # editor and serves stale in-memory code.
                if shutdown_event is not None:
                    shutdown_event.set()

            elif method == "session.implement":
                if session_manager is not None:
                    slug: str = str(params_dict.get("slug") or "default")
                    workspace: str = str(params_dict.get("workspace") or os.getcwd())
                    task: str = str(params_dict.get("task") or "")
                    opts: dict[str, Any] = {}
                    if params_dict.get("model"):
                        opts["model"] = str(params_dict["model"])
                    if params_dict.get("effort"):
                        opts["effort"] = str(params_dict["effort"])
                    effort_err = _effort_error(str(params_dict.get("effort") or ""))
                    if effort_err is not None:
                        await write_message(writer, _err(msg_id, -32602, effort_err))
                        continue
                    # Fail fast on an unsupported model before the worker starts —
                    # an unknown model otherwise hangs the turn ~600s. Skip when auth
                    # is unconfigured; the background turn surfaces that separately.
                    try:
                        auth_c = resolve_auth(None)
                    except AuthNotConfigured:
                        auth_c = None
                    if auth_c is not None:
                        compat_err = check_model(
                            str(params_dict.get("model") or auth_c.model)
                        )
                        if compat_err is not None:
                            await write_message(writer, _err(msg_id, -32602, compat_err))
                            continue
                        probe_err = await check_region_available(
                            auth_c, str(params_dict.get("model") or auth_c.model)
                        )
                        if probe_err is not None:
                            await write_message(writer, _err(msg_id, -32602, probe_err))
                            continue
                    if params_dict.get("files"):
                        try:
                            opts["files"] = resolve_attachments(
                                [str(f) for f in params_dict["files"]], workspace
                            )
                        except AttachmentError as exc:
                            await write_message(writer, _err(msg_id, -32010, str(exc)))
                            continue
                    if params_dict.get("resume"):
                        conv_arg = params_dict.get("conversation_id")
                        stored = session_store.get(
                            workspace, str(conv_arg) if conv_arg else None
                        )
                        if stored is not None:
                            opts["conversation_id"], opts["save_dir"] = stored
                    try:
                        session = await session_manager.create_session(slug, workspace, task)
                        session_manager.start_task(session.session_id, task, **opts)
                        await write_message(
                            writer,
                            _ok(msg_id, {"status": "started", "session_id": session.session_id}),
                        )
                    except SessionAlreadyActiveError:
                        logger.debug("broker: busy, rejecting session.implement slug=%s", slug)
                        await write_message(writer, _err(msg_id, -32001, "busy"))
                else:
                    # Legacy path — keeps test_ipc.py green
                    if session_event.is_set():
                        logger.debug("broker: busy, rejecting session.implement")
                        await write_message(writer, _err(msg_id, -32001, "busy"))
                    else:
                        session_event.set()
                        owns_event = True
                        logger.info("session acquired via session.implement")
                        await write_message(writer, _ok(msg_id, {"status": "started"}))

            elif method == "session.approve":
                if approval_waiter is not None:
                    call_id: str = str(params_dict.get("call_id") or "")
                    scope: str = str(params_dict.get("scope") or "once")
                    if not call_id:
                        await write_message(writer, _err(msg_id, -32602, "Invalid params: call_id required"))
                        continue
                    meta = approval_waiter.meta_for(call_id)  # read before resolve pops it
                    try:
                        approval_waiter.resolve(call_id, ApprovalDecision.ALLOW)
                    except UnknownCallIdError:
                        await write_message(writer, _err(msg_id, -32602, f"Unknown call_id: {call_id}"))
                        continue
                    if scope in ("project", "global") and meta is not None:
                        command_a, sid_a = meta
                        sess_a = (
                            session_manager.get_session(sid_a)
                            if session_manager is not None
                            else None
                        )
                        if sess_a is not None:
                            approval_store.remember(
                                command_a,
                                sess_a.workspace,
                                cast(approval_store.Scope, scope),
                            )
                    await write_message(writer, _ok(msg_id, {"approved": True}))
                else:
                    await write_message(writer, _err(msg_id, -32601, "Method not found"))

            elif method == "session.deny":
                if approval_waiter is not None:
                    call_id_d: str = str(params_dict.get("call_id") or "")
                    if not call_id_d:
                        await write_message(writer, _err(msg_id, -32602, "Invalid params: call_id required"))
                        continue
                    try:
                        approval_waiter.resolve(call_id_d, ApprovalDecision.DENY)
                        await write_message(writer, _ok(msg_id, {"denied": True}))
                    except UnknownCallIdError:
                        await write_message(writer, _err(msg_id, -32602, f"Unknown call_id: {call_id_d}"))
                else:
                    await write_message(writer, _err(msg_id, -32601, "Method not found"))

            elif method == "session.status":
                if session_manager is not None:
                    sid_s: str = str(params_dict.get("session_id") or "") or (
                        session_manager.active_session_id()
                        or session_manager.latest_session_id()
                        or ""
                    )
                    sess = session_manager.get_session(sid_s) if sid_s else None
                    if sess is not None:
                        pending = (
                            approval_waiter.pending_details(sid_s)
                            if approval_waiter is not None
                            else []
                        )
                        await write_message(
                            writer, _ok(msg_id, {"state": sess.state, "pending_approvals": pending})
                        )
                    else:
                        msg_s = f"Unknown session_id: {sid_s}" if sid_s else "No sessions exist"
                        await write_message(writer, _err(msg_id, -32602, msg_s))
                else:
                    await write_message(writer, _err(msg_id, -32601, "Method not found"))

            elif method == "session.wait" and session_manager is not None:
                # Bounded long-poll (~25s): return as soon as an approval is pending
                # or the session ends, so a supervisor can watch without busy-polling.
                sid_w: str = str(params_dict.get("session_id") or "") or (
                    session_manager.active_session_id()
                    or session_manager.latest_session_id()
                    or ""
                )
                if session_manager.get_session(sid_w) is None:
                    msg_w = f"Unknown session_id: {sid_w}" if sid_w else "No sessions exist"
                    await write_message(writer, _err(msg_id, -32602, msg_w))
                else:
                    payload_w: dict[str, Any] = {"kind": "running", "state": "running"}
                    for _ in range(50):
                        sess_w = session_manager.get_session(sid_w)
                        state_w = sess_w.state if sess_w else "gone"
                        pending_w = (
                            approval_waiter.pending_details(sid_w)
                            if approval_waiter is not None
                            else []
                        )
                        if pending_w:
                            payload_w = {
                                "kind": "approval",
                                "state": state_w,
                                "pending_approvals": pending_w,
                            }
                            break
                        if state_w in ("done", "crashed", "cancelled", "timed_out", "gone"):
                            payload_w = {"kind": "done", "state": state_w}
                            break
                        payload_w = {"kind": "running", "state": state_w}
                        await asyncio.sleep(0.5)
                    await write_message(writer, _ok(msg_id, payload_w))

            elif method == "session.cancel" and session_manager is not None:
                sid_c: str = str(params_dict.get("session_id") or "") or (
                    session_manager.active_session_id() or ""
                )
                await session_manager.cancel_session(sid_c)
                await write_message(writer, _ok(msg_id, {"cancelled": True}))

            elif method == "session.events" and session_manager is not None:
                sid_e: str = str(params_dict.get("session_id") or "")
                after_e: int = int(params_dict.get("after_event_id") or 0)
                evs = await session_manager.get_events(sid_e, after_e)
                await write_message(
                    writer,
                    _ok(msg_id, {"events": [e.model_dump(by_alias=True) for e in evs]}),
                )

            elif method == "session.retry" and session_manager is not None:
                sid_r: str = str(params_dict.get("session_id") or "") or (
                    session_manager.latest_session_id() or ""
                )
                strategy: str = str(params_dict.get("strategy") or "clean")
                last = session_manager.get_last_task(sid_r)
                if session_manager.get_session(sid_r) is None or last is None:
                    await write_message(writer, _err(msg_id, -32602, f"No retryable task for session_id: {sid_r}"))
                else:
                    # Replay original opts (model/effort/FILES) so a multimodal
                    # retry re-attaches its image; resume keeps the conversation,
                    # clean (default) starts fresh.
                    retry_opts = session_manager.get_task_opts(sid_r)
                    if strategy != "resume":
                        retry_opts.pop("conversation_id", None)
                    session_manager.transition(sid_r, "running")
                    session_manager.start_task(sid_r, f"[retry:{strategy}] {last}", **retry_opts)
                    await write_message(writer, _ok(msg_id, {"status": "retrying", "session_id": sid_r}))

            elif method == "session.review" and session_manager is not None:
                slug_v: str = str(params_dict.get("slug") or "default")
                workspace_v: str = str(params_dict.get("workspace") or os.getcwd())
                target: str = str(params_dict.get("target") or "")
                review_task = (
                    "Review the following for correctness, security, and risk. "
                    "Do NOT modify any files; report findings only.\n\n" + target
                )
                try:
                    session_v = await session_manager.create_session(slug_v, workspace_v, review_task)
                    session_manager.start_task(session_v.session_id, review_task, review=True)
                    await write_message(
                        writer,
                        _ok(msg_id, {"status": "started", "session_id": session_v.session_id}),
                    )
                except SessionAlreadyActiveError:
                    await write_message(writer, _err(msg_id, -32001, "busy"))

            elif method == "session.handoff" and session_manager is not None:
                slug_h: str = str(params_dict.get("slug") or "default")
                workspace_h: str = str(params_dict.get("workspace") or os.getcwd())
                target_h: str = str(params_dict.get("target") or "")
                transcript_path_h: str = str(params_dict.get("transcript_path") or "")
                summary_h = transcript.load_handoff_summary(transcript_path_h or None)
                handoff_task = target_h or (
                    "Continue the work described in the handed-off conversation summary."
                )
                try:
                    auth_h = resolve_auth(None)
                except AuthNotConfigured:
                    auth_h = None
                if auth_h is not None:
                    probe_err = await check_region_available(auth_h, auth_h.model)
                    if probe_err is not None:
                        await write_message(writer, _err(msg_id, -32602, probe_err))
                        continue
                try:
                    session_h = await session_manager.create_session(
                        slug_h, workspace_h, handoff_task
                    )
                    session_manager.start_task(
                        session_h.session_id, handoff_task, system_instructions=summary_h
                    )
                    await write_message(
                        writer,
                        _ok(msg_id, {"status": "started", "session_id": session_h.session_id}),
                    )
                except SessionAlreadyActiveError:
                    await write_message(writer, _err(msg_id, -32001, "busy"))

            elif method == "session.list" and session_manager is not None:
                await write_message(
                    writer, _ok(msg_id, {"sessions": session_manager.list_sessions()})
                )

            elif method.startswith("session.") and session_manager is None:
                # Unknown session.* in legacy (no-manager) mode → broker fallback.
                # With a manager present, unknown/removed methods (e.g. the removed
                # session.delegate) fall through to -32601 below — never a fake "started".
                if session_event.is_set():
                    logger.debug("broker: busy, rejecting %s", method)
                    await write_message(writer, _err(msg_id, -32001, "busy"))
                else:
                    session_event.set()
                    owns_event = True
                    logger.info("session acquired via %s", method)
                    await write_message(writer, _ok(msg_id, {"status": "started"}))

            else:
                await write_message(writer, _err(msg_id, -32601, "Method not found"))

    except (asyncio.CancelledError, ConnectionResetError):
        logger.debug("client connection cancelled or reset")
    finally:
        if owns_event:
            session_event.clear()
            logger.info("session released (connection closed)")
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def serve(sock_path: Path) -> None:
    """Bind *sock_path*, accept clients, and run until SIGTERM/SIGINT."""
    session_event: asyncio.Event = asyncio.Event()
    shutdown: asyncio.Event = asyncio.Event()
    waiter = ApprovalWaiter()
    from cao.runtime.event_bus import EventBus

    workspace = ws.resolve_workspace()
    state_dir = compute_state_dir(workspace)
    file_handler = logging.FileHandler(state_dir / "daemon.log")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    event_bus = EventBus(state_dir)
    from cao.runtime.digest_generator import DigestGenerator
    from cao.runtime.git_diff_collector import GitDiffCollector

    git_diff = GitDiffCollector(state_dir)
    digest_gen = DigestGenerator(state_dir)
    event_bus.subscribe(digest_gen.handle)

    mgr = SessionManager(
        approval_waiter=waiter,
        event_bus=event_bus,
        git_diff_collector=git_diff,
        digest_generator=digest_gen,
    )
    open_writers: list[asyncio.StreamWriter] = []

    async def _client_factory(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        open_writers.append(writer)
        try:
            await handle_client(
                reader,
                writer,
                session_event,
                session_manager=mgr,
                approval_waiter=waiter,
                shutdown_event=shutdown,
            )
        finally:
            try:
                open_writers.remove(writer)
            except ValueError:
                pass

    # BL-24: serialize cold starts under an flock so two near-simultaneous starts
    # can never both bind. Release the lock right after binding — holding it for the
    # daemon lifetime would deadlock the next start.
    lockfd = os.open(str(state_dir / "daemon.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        # ponytail: flock makes the winner's socket live before the loser pings, so
        # the pong window is ~µs; only pathological scheduler starvation (winner's
        # loop not run within _ping's 0.5s) could still double-bind. Ceiling
        # accepted; upgrade path: yield on connect-success (drop the pong wait).
        if _ping(sock_path, timeout=2.0):
            logger.info("daemon already serving %s; yielding", sock_path)
            return
        sock_path.unlink(missing_ok=True)  # only-if-stale, guarded by the lock
        (state_dir / "root").touch()  # BL-21: mark this resolved workspace a deliberate root
        server = await asyncio.start_unix_server(_client_factory, path=str(sock_path))
    finally:
        os.close(lockfd)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    loop.add_signal_handler(signal.SIGINT, shutdown.set)

    logger.info("daemon listening at %s", sock_path)

    try:
        await shutdown.wait()
    finally:
        logger.info("shutdown: closing %d open connection(s)", len(open_writers))
        server.close()
        for writer in list(open_writers):
            try:
                writer.close()
            except OSError:
                pass
        await server.wait_closed()
        sock_path.unlink(missing_ok=True)
        logger.info("socket removed; daemon stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(serve(socket_path()))


if __name__ == "__main__":
    main()
