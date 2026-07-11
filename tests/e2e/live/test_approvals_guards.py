"""LIVE e2e — "Approvals + Guards" domain (real Gemini via Vertex ADC).

Guarded by CAO_LIVE_TEST=1 (see conftest collect_ignore_glob). Each test gets a
fresh isolated git workspace + daemon via the make_driver fixture.

Approvals are tested through the REAL companion approve/deny path — no test-mode
bypass. To force the worker to hit run_command (which requires approval;
edit_file is auto-allowed), each task is phrased "Use the run_command tool to run
this EXACT command and nothing else: <cmd>", which also keeps the command_line
byte-stable so the exact-match allowlist (S21) can fire.

Isolation: CAO_PLUGIN_DATA is redirected to a temp dir at import time so the
allowlist (approvals.json) and daemon state never touch the real ~/.config/cao.

Scenarios:
  S20 approve once            -> command runs only after /agy:approve
  S21 allowlist (project)     -> identical command auto-approves (approval.auto_allowed)
  S22 deny                    -> command never runs; denial recorded
  S23 invalid effort          -> -32602, no session started
  S24 file outside workspace  -> -32010
  S25 secret .env file        -> -32010
  S26 unsupported MIME        -> -32010
"""
from __future__ import annotations

import os
import re
import tempfile

# Isolate the allowlist + state dir before any driver is built (drivers snapshot
# os.environ at construction). Both approval_store.store_path and
# daemon.compute_state_dir key off CAO_PLUGIN_DATA.
os.environ.setdefault("CAO_PLUGIN_DATA", tempfile.mkdtemp(prefix="agy-approvals-"))


def _call_id(raw: str) -> str:
    """Pull the pending approval's call_id from a status/pending render.

    The companion prints ready-to-paste lines like `/agy:approve 1`; the id is a
    short integer minted by ApprovalWaiter.next_call_id().
    """
    m = re.search(r"/agy:approve\s+(\S+)", raw)
    assert m, f"no pending call_id found in:\n{raw}"
    return m.group(1)


# --- S20 approve once -------------------------------------------------------
def test_s20_approve_once(make_driver) -> None:
    """S20: a run_command call suspends for approval; the side effect (marker.txt)
    appears only after /agy:approve resolves it."""
    d = make_driver()
    r = d.delegate(
        "Use the run_command tool to run this EXACT command and nothing else: "
        "echo hi > marker.txt",
        background=True,
    )
    assert r.ok, f"delegate failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id\n{r.raw}"

    p = d.poll(r.session_id, want_pending=True, timeout=120)
    assert not (d.ws / "marker.txt").exists(), "command ran BEFORE approval — gate leaked"
    cid = _call_id(p.raw)

    a = d.approve(cid)
    assert a.ok, f"approve failed: {a.error_code} {a.error_msg}\n{a.raw}"

    final = d.poll(r.session_id, timeout=200)
    assert final.state == "done", f"state={final.state}\n{final.raw}"
    assert (d.ws / "marker.txt").exists(), (
        f"marker.txt missing after approval; events={d.event_types(r.session_id)}"
    )


# --- S21 allowlist (project scope) -----------------------------------------
def test_s21_allowlist_project_auto_approve(make_driver) -> None:
    """S21: approving with scope 'project' remembers the exact command; an
    identical later command auto-approves — daemon emits approval.auto_allowed and
    never suspends. Both runs produce the side effect."""
    d = make_driver()
    task = (
        "Use the run_command tool to run this EXACT command and nothing else: "
        "echo a2 > allow.txt"
    )

    # Run 1: suspend, approve for the project.
    r1 = d.delegate(task, background=True)
    assert r1.ok and r1.session_id, f"run1 delegate failed:\n{r1.raw}"
    p1 = d.poll(r1.session_id, want_pending=True, timeout=120)
    cid = _call_id(p1.raw)
    a = d.approve(cid, scope="project")
    assert a.ok, f"approve project failed: {a.error_code} {a.error_msg}\n{a.raw}"
    f1 = d.poll(r1.session_id, timeout=200)
    assert f1.state == "done", f"run1 state={f1.state}\n{f1.raw}"
    assert (d.ws / "allow.txt").exists(), "run1 side effect missing after approval"
    (d.ws / "allow.txt").unlink()  # clear so run2's side effect is unambiguous

    # Run 2: identical command must auto-approve WITHOUT any pending suspension.
    r2 = d.delegate(task, background=True)
    assert r2.ok and r2.session_id, f"run2 delegate failed:\n{r2.raw}"
    f2 = d.poll(r2.session_id, timeout=200)
    assert f2.state == "done", f"run2 state={f2.state}\n{f2.raw}"

    types2 = d.event_types(r2.session_id)
    assert "approval.auto_allowed" in types2, (
        f"expected approval.auto_allowed on run2; got events={types2}"
    )
    assert "approval.required" not in types2, (
        f"run2 suspended for approval — allowlist did not short-circuit; events={types2}"
    )
    assert (d.ws / "allow.txt").exists(), "run2 side effect missing after auto-approve"


# --- S22 deny ---------------------------------------------------------------
def test_s22_deny(make_driver) -> None:
    """S22: denying a suspended run_command blocks the side effect and records a
    tool.denied event; denied.txt is never created."""
    d = make_driver()
    r = d.delegate(
        "Use the run_command tool to run this EXACT command and nothing else: "
        "echo NO > denied.txt",
        background=True,
    )
    assert r.ok and r.session_id, f"delegate failed:\n{r.raw}"

    p = d.poll(r.session_id, want_pending=True, timeout=120)
    cid = _call_id(p.raw)
    dn = d.deny(cid, "not allowed")
    assert dn.ok, f"deny failed: {dn.error_code} {dn.error_msg}\n{dn.raw}"

    # Keep denying any post-denial retry (each run_command re-suspends) until terminal.
    final = d.poll(r.session_id, timeout=200, auto_deny=True)
    assert final.state in ("done", "crashed"), f"state={final.state}\n{final.raw}"
    assert not (d.ws / "denied.txt").exists(), "denied command still ran — deny gate failed"
    assert "tool.denied" in d.event_types(r.session_id), (
        f"denial not recorded; events={d.event_types(r.session_id)}"
    )


# --- S23 invalid effort -----------------------------------------------------
def test_s23_invalid_effort(make_driver) -> None:
    """S23: an unknown --effort is rejected with -32602 before any session
    starts (valid levels: minimal/low/medium/high)."""
    d = make_driver()
    r = d.implement("do something", effort="extreme")
    assert r.error_code == -32602, f"expected -32602, got {r.error_code}\n{r.raw}"
    assert r.session_id is None, f"a session started despite bad effort\n{r.raw}"


# --- S24 file outside workspace ---------------------------------------------
def test_s24_file_outside_workspace(make_driver) -> None:
    """S24: an attachment path outside the workspace is rejected with -32010."""
    d = make_driver()
    r = d.implement("look at this", files=["/etc/hostname"])
    assert r.error_code == -32010, f"expected -32010, got {r.error_code}\n{r.raw}"
    assert r.session_id is None, f"a session started despite out-of-ws file\n{r.raw}"


# --- S25 secret file --------------------------------------------------------
def test_s25_secret_file(make_driver) -> None:
    """S25: an in-workspace secret file (.env) is rejected with -32010 by the
    secret-file deny inside resolve_attachments."""
    d = make_driver()
    (d.ws / ".env").write_text("SECRET=x\n")
    r = d.implement("read this", files=[".env"])
    assert r.error_code == -32010, f"expected -32010, got {r.error_code}\n{r.raw}"
    assert r.session_id is None, f"a session started despite secret file\n{r.raw}"


# --- S26 unsupported MIME ---------------------------------------------------
def test_s26_unsupported_mime(make_driver) -> None:
    """S26: an existing file whose type is not a supported SDK MIME is rejected
    with -32010 (the file must exist to reach the MIME check)."""
    d = make_driver()
    (d.ws / "bad.xyz").write_text("x\n")
    r = d.implement("look", files=["bad.xyz"])
    assert r.error_code == -32010, f"expected -32010, got {r.error_code}\n{r.raw}"
    assert r.session_id is None, f"a session started despite unsupported MIME\n{r.raw}"


# --- S21b allowlist (global scope, cross-workspace) -------------------------
def test_s21b_allowlist_global_cross_workspace(make_driver) -> None:
    """S21b: approving with scope 'global' remembers the command everywhere — an
    identical command in a DIFFERENT workspace auto-approves (the project-scope
    distinction: global crosses workspaces). Shared allowlist via CAO_PLUGIN_DATA."""
    task = (
        "Use the run_command tool to run this EXACT command and nothing else: "
        "echo g2 > glob.txt"
    )
    # Workspace A: suspend, approve globally.
    da = make_driver()
    ra = da.delegate(task, background=True)
    assert ra.ok and ra.session_id, f"A delegate failed:\n{ra.raw}"
    pa = da.poll(ra.session_id, want_pending=True, timeout=120)
    a = da.approve(_call_id(pa.raw), scope="global")
    assert a.ok, f"approve global failed: {a.error_code} {a.error_msg}\n{a.raw}"
    assert da.poll(ra.session_id, timeout=200).state == "done"

    # Workspace B (different dir, same global allowlist): identical command auto-approves.
    db = make_driver()
    rb = db.delegate(task, background=True)
    assert rb.ok and rb.session_id, f"B delegate failed:\n{rb.raw}"
    assert db.poll(rb.session_id, timeout=200).state == "done"
    types_b = db.event_types(rb.session_id)
    assert "approval.auto_allowed" in types_b, (
        f"global approval did not cross into workspace B; events={types_b}"
    )
    assert "approval.required" not in types_b, (
        f"workspace B suspended — global allowlist did not apply; events={types_b}"
    )
    assert (db.ws / "glob.txt").exists(), "B side effect missing after global auto-approve"
