"""Unit and integration tests for IPC framing and daemon dispatch.

Coverage required by AC1:
    - read_message / write_message round-trip with a valid JSON-RPC object.
    - read_message raises FramingError on a non-JSON line.
    - read_message raises FramingError on a truncated stream (EOF before newline).
    - Full ping/pong round-trip through a real asyncio Unix socket.
    - Second concurrent session.implement while a session is active → -32001.

Additional adversarial coverage (per task spec):
    - Garbage / non-JSON input over the socket → -32700 parse error, no crash.
    - Missing ``method`` field → -32600 invalid request.
    - Hung client (connects, sends nothing) does not block other clients.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket as _socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from cao.runtime import workspace as ws
from cao.runtime.daemon import compute_state_dir, handle_client
from cao.runtime.ipc import FramingError, read_message, write_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_event() -> asyncio.Event:
    """Return a new, un-set asyncio.Event."""
    return asyncio.Event()


async def _start_server(
    sock_path: Path,
    session_event: asyncio.Event,
) -> asyncio.Server:
    """Start a Unix socket server backed by handle_client."""
    return await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, session_event),
        path=str(sock_path),
    )


# ---------------------------------------------------------------------------
# Framing unit tests (no network — feed data directly into StreamReader)
# ---------------------------------------------------------------------------


async def test_read_write_roundtrip(tmp_path: Path) -> None:
    """write_message then read_message preserves the JSON-RPC object exactly."""
    sock_path = tmp_path / "rtt.sock"

    async def echo(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        msg = await read_message(reader)
        await write_message(writer, msg)
        writer.close()

    server = await asyncio.start_unix_server(echo, path=str(sock_path))
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock_path))
        obj = {"jsonrpc": "2.0", "id": 42, "result": "pong"}
        await write_message(w, obj)
        result = await read_message(r)
        assert result == obj
        w.close()
        await w.wait_closed()


async def test_read_message_bad_json() -> None:
    """read_message raises FramingError on a non-JSON line."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"not-valid-json\n")
    with pytest.raises(FramingError):
        await read_message(reader)


async def test_read_message_eof_empty() -> None:
    """read_message raises FramingError when the stream is closed with no data."""
    reader = asyncio.StreamReader()
    reader.feed_eof()
    with pytest.raises(FramingError):
        await read_message(reader)


async def test_read_message_truncated() -> None:
    """read_message raises FramingError on EOF before a newline (truncated frame)."""
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"partial": true')  # no trailing \n
    reader.feed_eof()
    with pytest.raises(FramingError):
        await read_message(reader)


# ---------------------------------------------------------------------------
# Integration: ping / pong round-trip through a real Unix socket (AC2 gate)
# ---------------------------------------------------------------------------


async def test_ping_pong_round_trip(tmp_path: Path) -> None:
    """Full ping/pong through a real asyncio Unix socket returns exactly 'pong'."""
    sock_path = tmp_path / "ping.sock"
    session_event = _fresh_event()
    server = await _start_server(sock_path, session_event)

    async with server:
        r, w = await asyncio.open_unix_connection(str(sock_path))
        await write_message(w, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        response = await read_message(r)
        # Assert actual bytes decoded — not a log line.
        assert response == {"jsonrpc": "2.0", "id": 1, "result": "pong"}
        w.close()
        await w.wait_closed()


# ---------------------------------------------------------------------------
# Integration: broker pattern — second concurrent session request → -32001
# ---------------------------------------------------------------------------


async def test_busy_on_concurrent_session(tmp_path: Path) -> None:
    """Second session.implement while first is active returns -32001 busy."""
    sock_path = tmp_path / "busy.sock"
    session_event = _fresh_event()
    server = await _start_server(sock_path, session_event)

    async with server:
        # Client A acquires the session; connection stays open (owns_session=True).
        r1, w1 = await asyncio.open_unix_connection(str(sock_path))
        await write_message(w1, {"jsonrpc": "2.0", "id": 1, "method": "session.implement"})
        resp1 = await read_message(r1)
        assert resp1.get("result", {}).get("status") == "started"

        # Client B: session is still active → must receive -32001.
        r2, w2 = await asyncio.open_unix_connection(str(sock_path))
        await write_message(w2, {"jsonrpc": "2.0", "id": 2, "method": "session.implement"})
        resp2 = await read_message(r2)
        assert resp2["error"]["code"] == -32001
        assert resp2["error"]["message"] == "busy"

        w1.close()
        w2.close()
        await asyncio.gather(w1.wait_closed(), w2.wait_closed(), return_exceptions=True)


# ---------------------------------------------------------------------------
# Adversarial: malformed / missing-field input must not crash the daemon
# ---------------------------------------------------------------------------


async def test_garbage_input_returns_parse_error(tmp_path: Path) -> None:
    """Garbage bytes → JSON-RPC -32700 parse error; daemon keeps running."""
    sock_path = tmp_path / "garbage.sock"
    server = await _start_server(sock_path, _fresh_event())

    async with server:
        r, w = await asyncio.open_unix_connection(str(sock_path))
        w.write(b"totally not json\n")
        await w.drain()
        raw = await r.readline()
        resp = json.loads(raw)
        assert resp["error"]["code"] == -32700
        # Daemon is still up — send a valid ping right after.
        await write_message(w, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        pong = await read_message(r)
        assert pong["result"] == "pong"
        w.close()
        await w.wait_closed()


async def test_missing_method_returns_invalid_request(tmp_path: Path) -> None:
    """Missing 'method' field → -32600 Invalid Request."""
    sock_path = tmp_path / "nmethod.sock"
    server = await _start_server(sock_path, _fresh_event())

    async with server:
        r, w = await asyncio.open_unix_connection(str(sock_path))
        # Valid JSON but no 'method' key.
        await write_message(w, {"jsonrpc": "2.0", "id": 1})
        resp = await read_message(r)
        assert resp["error"]["code"] == -32600
        w.close()
        await w.wait_closed()


async def test_hung_client_does_not_block_others(tmp_path: Path) -> None:
    """A client that never sends allows other clients to be served normally."""
    sock_path = tmp_path / "hung.sock"
    server = await _start_server(sock_path, _fresh_event())

    async with server:
        # Hung client: connect but send nothing (handle_client suspends on readline).
        _r_hung, w_hung = await asyncio.open_unix_connection(str(sock_path))

        # Active client: should be served without waiting for the hung client.
        r2, w2 = await asyncio.open_unix_connection(str(sock_path))
        await write_message(w2, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        resp = await read_message(r2)
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": "pong"}

        w_hung.close()
        w2.close()
        await asyncio.gather(
            w_hung.wait_closed(), w2.wait_closed(), return_exceptions=True
        )


# ---------------------------------------------------------------------------
# BL-24: daemon cold-start split-brain — startup must be race-safe.
# Real serve() runs in its own process: a synchronous _ping inside serve()
# would deadlock one shared event loop, so separate processes (independent
# loops + real cross-process flock) are the faithful harness.
# ---------------------------------------------------------------------------


def _daemon_sock() -> Path:
    """Socket path the daemon resolves from the current env (CAO_WORKSPACE)."""
    return compute_state_dir(ws.resolve_workspace()) / "rpc.sock"


def _spawn_daemon() -> subprocess.Popen[bytes]:
    """Spawn a real ``serve()`` in its own process (own loop + own flock attempt)."""
    return subprocess.Popen(
        [sys.executable, "-m", "cao.runtime.daemon"],
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ping_daemon(sock: Path, timeout: float = 0.5) -> bool:
    """True iff a live daemon answers ping on *sock* (mirrors _is_daemon_alive)."""
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as c:
            c.settimeout(timeout)
            c.connect(str(sock))
            c.sendall(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
        raw = json.loads(buf.split(b"\n")[0])
        return isinstance(raw, dict) and raw.get("result") == "pong"
    except (OSError, ValueError):
        return False


def _wait_until(cond: Callable[[], bool], timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _terminate(*procs: subprocess.Popen[bytes]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)


def test_cold_start_second_instance_yields_to_live_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: a second cold-start serve() over a live daemon yields — single instance.

    Given: a daemon is already up on a cold (empty) state dir.
    When:  a second ``serve()`` cold-starts on the same state dir + socket.
    Then:  the flock + liveness check make it detect the live socket and exit
           cleanly (rc 0) instead of unlinking + rebinding — exactly one daemon
           stays bound (no split-brain).

    Deterministic by construction: d1 is fully alive (answers ping) before d2
    starts, so d2's in-serve() ping hits an already-serving loop — no timing
    window, no flake. (True-simultaneous double-bind is prevented by the OS-atomic
    flock; the invariant asserted here is that a second start never orphans the
    first. The narrow starvation ceiling is noted in daemon.serve().)
    """
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path))
    sock = _daemon_sock()

    d1 = _spawn_daemon()
    try:
        assert _wait_until(lambda: _ping_daemon(sock), timeout=20.0), (
            "first daemon must come up and answer ping"
        )
        d2 = _spawn_daemon()
        try:
            # On the split-brain bug d2 unconditionally unlinks d1's socket and
            # rebinds → d2 runs forever. The fix makes d2 detect d1 and yield.
            assert _wait_until(lambda: d2.poll() is not None, timeout=20.0), (
                "second serve() over a live daemon must exit; it kept running → it "
                "rebound and orphaned the original (split-brain)"
            )
            assert d2.returncode == 0, (
                f"the yielding daemon must exit cleanly, got rc={d2.returncode}"
            )
            assert d1.poll() is None, "the original daemon must stay alive"
            assert _ping_daemon(sock), "the surviving daemon must answer ping"
        finally:
            _terminate(d2)
    finally:
        _terminate(d1)


def test_crash_respawn_reconnects_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S9: respawn rebinds a stale socket, but a respawn over a LIVE daemon reconnects.

    Given: a crashed daemon left a stale (dead) socket file behind.
    When:  a daemon is (re)spawned.
    Then:  it detects the socket is dead, unlinks it, and binds fresh (recovers).
    And when a *second* respawn fires while that daemon is alive,
    Then:  it detects the live socket and exits cleanly WITHOUT unlinking/rebinding —
           the original daemon's socket is preserved (same inode), so no orphan
           daemon is left writing to the same state dir.
    """
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path))
    sock = _daemon_sock()

    # Crash artifact: a bound-then-closed socket file with no listener behind it.
    stale = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    stale.bind(str(sock))
    stale.close()
    assert sock.exists() and not _ping_daemon(sock), "stale socket must look dead"

    respawn = _spawn_daemon()
    try:
        assert _wait_until(lambda: _ping_daemon(sock)), (
            "respawn must rebind the dead socket and become alive"
        )
        inode_before = os.stat(sock).st_ino

        # Spurious respawn while the daemon is alive (companion liveness flap).
        second = _spawn_daemon()
        try:
            exited = _wait_until(lambda: second.poll() is not None)
            assert exited, (
                "respawn over a live daemon must exit cleanly; it kept running → it "
                "rebound and orphaned the original daemon (split-brain)"
            )
            assert second.returncode == 0, (
                f"the reconnecting respawn must exit 0, got rc={second.returncode}"
            )
            assert respawn.poll() is None, "the original daemon must stay alive"
            assert os.stat(sock).st_ino == inode_before, (
                "original daemon's socket must be preserved; a new inode means the "
                "respawn unlinked + rebound it (orphaned the original)"
            )
            assert _ping_daemon(sock), "the original daemon must still answer ping"
        finally:
            _terminate(second)
    finally:
        _terminate(respawn)
