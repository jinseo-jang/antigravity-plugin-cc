"""Live e2e tests — Core + Effort domain (real Gemini via Vertex ADC).

Path exercised: claude-code -> companion CLI -> daemon -> real Gemini worker.
Guarded by conftest collect_ignore_glob (needs CAO_LIVE_TEST=1).

Observability note: the daemon path does not surface worker chat text, so every
"answer" is verified via a file the worker is told to write, or via the
workspace-isolated events.jsonl (session.model / session.ended payloads).
"""
from __future__ import annotations

import pytest

from .agy_driver import AgyDriver


def _model_events(d: AgyDriver, sid: str) -> list[dict]:
    return [e for e in d.read_events(sid) if e.get("type") == "session.model"]


def test_s1_implement_basic(make_driver) -> None:
    """S1: implement writes a file and emits session.model; session reaches done."""
    d = make_driver(location="global")
    r = d.implement("Create a file named s1.txt containing exactly S1-OK using your file tool")
    assert r.ok, f"implement failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id, f"no session_id in output:\n{r.raw}"

    final = d.poll(r.session_id, auto_approve=True)
    assert final.state == "done", f"expected done, got {final.state}\n{final.raw}"

    f = d.ws / "s1.txt"
    assert f.exists(), "s1.txt not created"
    assert "S1-OK" in f.read_text(), f"unexpected content: {f.read_text()!r}"
    assert "session.model" in d.event_types(r.session_id)


def test_s2_model_flag(make_driver) -> None:
    """S2: --model is honored; session.model payload carries the requested model."""
    d = make_driver(location="global")
    r = d.implement("Create a file s2.txt with OK", model="gemini-3.1-pro-preview")
    assert r.ok, f"implement failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id

    final = d.poll(r.session_id, auto_approve=True)
    assert final.state == "done", f"expected done, got {final.state}\n{final.raw}"

    evs = _model_events(d, r.session_id)
    assert evs, "no session.model event"
    assert any("gemini-3.1-pro-preview" in str(e["payload"].get("model", "")) for e in evs), (
        f"gemini-3.1-pro-preview not in session.model payloads: {[e['payload'] for e in evs]}"
    )
    assert (d.ws / "s2.txt").exists(), "s2.txt not created"


@pytest.mark.parametrize(
    ("scenario", "effort"),
    [("s3", "minimal"), ("s4", "low"), ("s5", "medium"), ("s6", "high")],
)
def test_s3_s6_effort(make_driver, scenario: str, effort: str) -> None:
    """S3-S6: --effort maps to thinking_level (Gemini-3 + global) and creates the file."""
    d = make_driver(location="global")
    r = d.implement(
        f"Create a file named {scenario}.txt with content OK using your file tool."
        " Do not run any shell commands.",
        model="gemini-3.5-flash",
        effort=effort,
    )
    assert r.ok, f"implement failed: {r.error_code} {r.error_msg}\n{r.raw}"
    assert r.session_id

    final = d.poll(r.session_id, auto_approve=True)
    assert final.state == "done", f"expected done, got {final.state}\n{final.raw}"

    evs = _model_events(d, r.session_id)
    assert evs, "no session.model event"
    assert any(e["payload"].get("effort") == effort for e in evs), (
        f"effort {effort!r} not in session.model payloads: {[e['payload'] for e in evs]}"
    )
    assert (d.ws / f"{scenario}.txt").exists(), f"{scenario}.txt not created"


def test_s27_unsupported_model_rejected(make_driver) -> None:
    """S27: a non-allowlisted model is rejected pre-start with -32602 (fail-fast, no
    hang) — the guarantee that replaced the old 'effort on a Gemini-2 model hangs' path."""
    d = make_driver(location="global")
    r = d.implement("Reply with OK", model="gemini-2.5-flash")
    assert not r.ok, f"expected rejection, got ok:\n{r.raw}"
    assert r.error_code == -32602, f"expected -32602, got {r.error_code}\n{r.raw}"
    assert "Unsupported model" in (r.error_msg or ""), f"bad msg: {r.error_msg!r}\n{r.raw}"
