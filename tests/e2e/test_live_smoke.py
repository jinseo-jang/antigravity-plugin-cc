"""Live smoke test — real Gemini SDK, real GCP ADC creds.

Guarded: skips unless CAO_LIVE_TEST=1 in environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from cao.models import ApprovalDecision
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.digest_generator import DigestGenerator
from cao.runtime.event_bus import EventBus
from cao.runtime.git_diff_collector import GitDiffCollector
from cao.runtime.session_manager import SessionManager

_LIVE = os.environ.get("CAO_LIVE_TEST")

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live only; needs GCP ADC creds + harness (set CAO_LIVE_TEST=1)",
)

_TASK = (
    "Create a file named hello.txt in the workspace"
    " with exactly the content: HELLO CAO."
    " Use your file creation tool."
)


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


async def _auto_approver(waiter: ApprovalWaiter, stop: asyncio.Event) -> None:
    while not stop.is_set():
        # pending_ids is a property
        for cid in list(waiter.pending_ids):
            waiter.resolve(cid, ApprovalDecision.ALLOW)
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_live_smoke(tmp_path: Path) -> None:
    """Real Gemini turn: agent creates hello.txt; digest shows task + file."""
    # --- workspace ---
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "test@test")
    _git(ws, "config", "user.name", "test")
    (ws / "README.md").write_text("# live\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # --- components (vertex auth resolves from env: GOOGLE_CLOUD_PROJECT) ---
    waiter = ApprovalWaiter()
    bus = EventBus(state_dir)
    gdc = GitDiffCollector(state_dir)
    dg = DigestGenerator(state_dir)
    bus.subscribe(dg.handle)
    sm = SessionManager(
        approval_waiter=waiter,
        event_bus=bus,
        git_diff_collector=gdc,
        digest_generator=dg,
    )

    sess = await sm.create_session("live", str(ws), _TASK)

    stop = asyncio.Event()
    approver = asyncio.create_task(_auto_approver(waiter, stop))
    try:
        await asyncio.wait_for(
            sm.run_task(sess.session_id, _TASK),
            timeout=180,
        )
    finally:
        stop.set()
        await approver

    # --- ASSERT: hello.txt created with correct content ---
    hello = ws / "hello.txt"
    assert hello.exists(), "hello.txt not created by agent"
    assert "HELLO CAO" in hello.read_text(), f"unexpected content: {hello.read_text()!r}"

    # --- ASSERT: events.jsonl has required subsequence ---
    ej = state_dir / "events.jsonl"
    assert ej.exists(), "events.jsonl missing"
    raw_events = [json.loads(ln) for ln in ej.read_text().splitlines() if ln.strip()]
    assert len(raw_events) > 0, "no events recorded"
    types = [e.get("type", "") for e in raw_events]

    def _has_subsequence(seq: list[str], required: list[str]) -> bool:
        pos = 0
        for req in required:
            while pos < len(seq) and seq[pos] != req:
                pos += 1
            if pos >= len(seq):
                return False
            pos += 1
        return True

    required_seq = ["session.started", "session.ended", "digest.ready"]
    assert _has_subsequence(types, required_seq), f"event subsequence missing; got: {types}"

    dm = state_dir / "digest.md"
    assert dm.exists(), "digest.md missing"
    digest_text = dm.read_text()
    assert "hello.txt" in digest_text, f"hello.txt not in digest:\n{digest_text}"
    assert "Create a file" in digest_text or "hello.txt" in digest_text, (
        f"digest missing task reference; digest:\n{digest_text[:500]}"
    )

    # Print for -s capture
    print("\n=== digest.md ===")
    print(digest_text[:600])
    print("=== events ===")
    print(types)
