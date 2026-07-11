"""Live e2e tests — Lifecycle + Status domain (real Gemini via Vertex ADC).

Path exercised: claude-code -> companion CLI -> daemon -> real Gemini worker.
Guarded by conftest collect_ignore_glob (needs CAO_LIVE_TEST=1).

Observability note: the daemon path does not surface worker chat text, so recall
is verified via a file the worker is told to write into the workspace (``d.ws``),
never by scraping chat output.

Scenarios: S11 delegate --resume recall, S15 status, S16 events + after_event_id,
S17 watch (surfaces a pending approval), S18 retry happy-path + S18b no-session,
S19 cancel.

Contracts asserted (read from src/cao/runtime/daemon.py + session_manager.py):
- session.status(sid) -> {state, pending_approvals}; status("") -> err -32602
  "Unknown session_id:" (companion renders "Antigravity error -32602: ...").
- session.events filters ``event.id > after_event_id`` (event_bus._read_events).
- session.wait -> {kind: running|approval|done, state}; the approval kind carries
  pending_approvals rendered as "/agy:approve <id>" lines.
- session.retry with no id resolves latest_session_id() and re-runs "[retry:<strategy>]
  <last task>" -> {status: retrying, session_id}; -32602 only when no session exists.
- session.cancel -> {cancelled: true}; with no id resolves active_session_id();
  cancel_session transitions to "cancelled" and frees the active slug slot.
"""
from __future__ import annotations

import json
import time

from .agy_driver import AgyDriver

_VALID_STATES = {"idle", "running", "suspended", "done", "cancelled", "crashed", "timed_out"}


def _event_ids(raw: str) -> list[int]:
    """Extract event ids from a rendered session.events JSON blob."""
    try:
        data = json.loads(raw[raw.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return []
    return [e["id"] for e in data.get("events", []) if isinstance(e, dict) and "id" in e]


def test_s11_resume_recall(make_driver) -> None:
    """S11: --resume restores conversation_id+save_dir so turn2 recalls turn1's secret."""
    d: AgyDriver = make_driver()

    r1 = d.delegate(
        "Remember the secret word MANGO. Create noted.txt containing exactly noted "
        "using your file tool"
    )
    assert r1.ok, f"turn1 delegate failed: {r1.error_code} {r1.error_msg}\n{r1.raw}"
    assert r1.session_id, f"no session_id in turn1:\n{r1.raw}"
    final1 = d.poll(r1.session_id, auto_approve=True)
    assert final1.state == "done", f"turn1 expected done, got {final1.state}\n{final1.raw}"
    assert (d.ws / "noted.txt").exists(), "noted.txt not created in turn1"

    r2 = d.delegate(
        "Write the secret word you were told earlier into recall.txt using your "
        "file tool; if you don't know it, write UNKNOWN",
        resume=True,
    )
    assert r2.ok, f"turn2 delegate failed: {r2.error_code} {r2.error_msg}\n{r2.raw}"
    assert r2.session_id, f"no session_id in turn2:\n{r2.raw}"
    final2 = d.poll(r2.session_id, auto_approve=True)
    assert final2.state == "done", f"turn2 expected done, got {final2.state}\n{final2.raw}"

    recall = d.ws / "recall.txt"
    assert recall.exists(), "recall.txt not created in turn2"
    content = recall.read_text()
    assert "MANGO" in content, (
        f"--resume did not restore memory: recall.txt = {content!r}"
    )


def test_s15_status(make_driver) -> None:
    """S15: status(sid) reports a valid state; status('') fails gracefully with a message."""
    d: AgyDriver = make_driver()
    r = d.delegate("Create s15.txt containing S15-OK using your file tool")
    assert r.ok, f"delegate failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id:\n{r.raw}"

    st = d.status(r.session_id)
    assert st.ok, f"status(sid) errored: {st.error_code} {st.error_msg}\n{st.raw}"
    assert st.state in _VALID_STATES, f"invalid state {st.state!r}\n{st.raw}"

    # status("") — no id. Daemon contract: -32602 with an "Unknown session_id" message
    # (graceful, not a crash). Assert the exact code, not a loose OR.
    st_none = d.status("")
    assert st_none.error_code == -32602, (
        f"status('') must return -32602, got code={st_none.error_code}:\n{st_none.raw}"
    )
    assert "session_id" in st_none.raw.lower(), f"missing 'session_id' in message:\n{st_none.raw}"

    d.poll(r.session_id, auto_approve=True)  # drain to terminal for clean teardown


def test_s16_events_after_filter(make_driver) -> None:
    """S16: events are recorded; after_event_id reduces the returned set."""
    d: AgyDriver = make_driver()
    r = d.delegate("Create s16.txt containing S16-OK using your file tool")
    assert r.ok, f"delegate failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id
    final = d.poll(r.session_id)
    assert final.state == "done", f"expected done, got {final.state}\n{final.raw}"

    # On-disk baseline (events.jsonl) is non-empty for a completed session.
    disk = d.read_events(r.session_id)
    assert disk, "events.jsonl had no events for the session"

    ev_all = d.events(r.session_id)
    assert ev_all.ok, f"events(sid) errored:\n{ev_all.raw}"
    ids_all = _event_ids(ev_all.raw)
    assert ids_all, f"events(sid) returned no events:\n{ev_all.raw}"

    after = ids_all[0]  # filter out at least the first event
    ev_after = d.events(r.session_id, after=after)
    assert ev_after.ok, f"events(sid, after) errored:\n{ev_after.raw}"
    ids_after = _event_ids(ev_after.raw)
    assert len(ids_after) < len(ids_all), (
        f"after_event_id={after} did not reduce the set: "
        f"all={ids_all} after={ids_after}"
    )
    assert all(i > after for i in ids_after), (
        f"after filter leaked ids <= {after}: {ids_after}"
    )


def test_s17_watch_surfaces_pending_approval(make_driver) -> None:
    """S17: watch's headline job — a run_command suspends the session and `wait`
    surfaces the pending approval (daemon `kind:"approval"` branch), then converges
    to done once approved."""
    d: AgyDriver = make_driver()
    r = d.delegate(
        "Use the run_command tool to run this EXACT command and nothing else: echo hi > w.txt",
        background=True,
    )
    assert r.ok, f"delegate failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id

    deadline = time.time() + 150
    surfaced = False
    while time.time() < deadline:
        w = d.wait(r.session_id)
        assert w.ok, f"wait errored: {w.error_code} {w.error_msg}\n{w.raw}"
        if d.pending_ids(w.raw):
            surfaced = True
            break
        if "session finished" in w.raw.lower():
            break
    assert surfaced, "watch never surfaced the pending approval (kind:approval branch)"

    # Approve what watch surfaced, then confirm the command actually ran.
    for cid in d.pending_ids(d.status(r.session_id).raw):
        d.approve(cid)
    final = d.poll(r.session_id, auto_approve=True)
    assert final.state == "done", f"expected done after approval, got {final.state}\n{final.raw}"
    assert (d.ws / "w.txt").exists(), "approved command did not run"


def test_s18_retry_happy_path(make_driver) -> None:
    """S18: /agy:retry [strategy] with no id resolves the last session and re-runs its
    task. Proven by deleting the produced file, then retry re-creating it."""
    d: AgyDriver = make_driver()
    r = d.delegate("Create s18.txt containing S18-OK using your file tool")
    assert r.ok and r.session_id, f"delegate failed:\n{r.raw}"
    assert d.poll(r.session_id, auto_approve=True).state == "done"
    artifact = d.ws / "s18.txt"
    assert artifact.exists(), "first run did not create s18.txt"
    artifact.unlink()  # remove it so re-creation proves the retry actually re-ran

    rt = d.retry("clean")  # no session_id — daemon resolves latest_session_id()
    assert rt.ok, f"retry did not start (bug: happy-path unreachable): {rt.error_code} {rt.error_msg}\n{rt.raw}"
    assert "retrying" in rt.raw.lower(), f"expected 'retrying' status:\n{rt.raw}"
    assert rt.session_id == r.session_id, f"retry resolved the wrong session:\n{rt.raw}"

    assert d.poll(r.session_id, auto_approve=True).state == "done"
    assert artifact.exists() and "S18-OK" in artifact.read_text(), (
        "retry did not re-run the task (s18.txt not re-created)"
    )


def test_s18b_retry_no_session(make_driver) -> None:
    """S18b: retry on a workspace with no session -> graceful -32602, no crash."""
    d: AgyDriver = make_driver()
    rt = d.retry("clean")
    assert rt.error_code == -32602, f"expected -32602 'no retryable task', got:\n{rt.raw}"
    assert "no retryable task" in rt.raw.lower(), f"unexpected message:\n{rt.raw}"


def test_s19_cancel(make_driver) -> None:
    """S19: cancel a running session -> state 'cancelled'; the active slot frees."""
    d: AgyDriver = make_driver()
    r = d.delegate(
        "Count slowly from 1 to 50, writing each number on its own line, then "
        "create done.txt containing DONE using your file tool"
    )
    assert r.ok, f"delegate failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id:\n{r.raw}"

    c = d.cancel(r.session_id)
    assert c.ok, f"cancel errored: {c.error_code} {c.error_msg}\n{c.raw}"

    # Poll briefly for the cancelled transition (cancel_session sets it synchronously).
    deadline = time.time() + 30
    st = d.status(r.session_id)
    while time.time() < deadline and st.state != "cancelled":
        time.sleep(1)
        st = d.status(r.session_id)
    assert st.state == "cancelled", f"expected cancelled, got {st.state}\n{st.raw}"

    # Slot freed: a new delegate on the same workspace/slug is accepted (not busy).
    # Use a longer task so it is still active for the no-id cancel below.
    r2 = d.delegate(
        "Count slowly from 1 to 40, one number per line, then create freed.txt "
        "containing FREED using your file tool"
    )
    assert r2.ok and r2.session_id, (
        f"active slot not freed after cancel: {r2.error_code} {r2.error_msg}\n{r2.raw}"
    )

    # cancel() with NO id must resolve the active session (spec 004: cancel {} = active).
    c2 = d.cancel()
    assert c2.ok, f"no-id cancel errored: {c2.error_code} {c2.error_msg}\n{c2.raw}"
    deadline2 = time.time() + 30
    st2 = d.status(r2.session_id)
    while time.time() < deadline2 and st2.state != "cancelled":
        time.sleep(1)
        st2 = d.status(r2.session_id)
    assert st2.state == "cancelled", (
        f"no-id cancel did not resolve the active session; got {st2.state}\n{st2.raw}"
    )


def test_s11b_resume_with_explicit_id(make_driver) -> None:
    """S11b: --resume <session_id> (explicit id form, not bare --resume) restores
    that specific session's conversation."""
    d: AgyDriver = make_driver()
    r1 = d.delegate(
        "Remember the secret word KIWI. Create noted2.txt containing noted using your file tool"
    )
    assert r1.ok and r1.session_id, f"turn1 failed:\n{r1.raw}"
    assert d.poll(r1.session_id, auto_approve=True).state == "done"

    r2 = d.delegate(
        "Write the secret word you were told earlier into recall2.txt using your file tool",
        resume=r1.session_id,
    )
    assert r2.ok and r2.session_id, f"turn2 failed:\n{r2.raw}"
    assert d.poll(r2.session_id, auto_approve=True).state == "done"
    recall = d.ws / "recall2.txt"
    assert recall.exists(), "recall2.txt not created"
    assert "KIWI" in recall.read_text(), (
        f"--resume <id> did not restore that session's memory: {recall.read_text()!r}"
    )
