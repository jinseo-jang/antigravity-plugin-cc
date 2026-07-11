"""Tests for DigestGenerator: 6 sections, truncation, no_git_repo, risk flags."""

from __future__ import annotations

from pathlib import Path


from cao.models import (
    DiffSummary,
    FileChange,
    RiskFlag,
    RuntimeEvent,
    SECRET_PATH_MASK,
)
from cao.runtime.digest_generator import DigestGenerator, _DIFF_TRUNCATE_BYTES


def _make_event(
    session_id: str,
    event_type: str,
    payload: dict,  # type: ignore[type-arg]
    event_id: int = 1,
) -> RuntimeEvent:
    from datetime import datetime, timezone

    return RuntimeEvent(
        id=event_id,
        session_id=session_id,
        event_type=event_type,
        timestamp_utc=datetime.now(timezone.utc),
        payload=payload,
    )


def _simple_diff(tmp_path: Path, *, patch_content: bytes = b"") -> DiffSummary:
    patch_path = None
    if patch_content:
        p = tmp_path / "diff.patch"
        p.write_bytes(patch_content)
        patch_path = str(p)
    return DiffSummary(
        base_commit="abc1234",
        diff_stat=" foo.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)",
        diff_name_status="M\tfoo.py",
        changed_files=[FileChange(path="foo.py", status="M")],
        untracked_files=[],
        deleted_files=[],
        patch_path=patch_path,
    )


async def test_all_six_sections_present(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s1"
    await gen.handle(_make_event(sid, "session.started", {"task": "refactor"}))
    ds = _simple_diff(tmp_path)
    result = gen.render(sid, ds)
    md = result.markdown
    assert "# Session Digest" in md
    assert "## Changed Files" in md
    assert "## Tests" in md
    assert "## Policy Decisions" in md
    assert "## Risk Notes" in md
    assert "## Suggested Claude Review" in md


async def test_summary_section_contains_session_id(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "unique-sess-42"
    await gen.handle(_make_event(sid, "session.started", {"task": "lint"}))
    result = gen.render(sid, _simple_diff(tmp_path))
    assert sid in result.markdown


async def test_changed_files_shows_diff_stat(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s2"
    ds = _simple_diff(tmp_path)
    result = gen.render(sid, ds)
    assert "1 file changed" in result.markdown


async def test_no_git_repo_warning_in_digest(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s3"
    ds = DiffSummary(no_git_repo=True)
    result = gen.render(sid, ds)
    assert "git binary is not available" in result.markdown


async def test_large_diff_truncated_in_digest(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s4"
    big_patch = b"x" * (_DIFF_TRUNCATE_BYTES + 1000)
    ds = _simple_diff(tmp_path, patch_content=big_patch)
    result = gen.render(sid, ds)
    assert "[Diff truncated." in result.markdown
    # Full patch still on disk
    assert len(Path(ds.patch_path).read_bytes()) > _DIFF_TRUNCATE_BYTES  # type: ignore[arg-type]


async def test_small_diff_not_truncated(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s5"
    small_patch = b"diff --git a/foo.py b/foo.py\n+hello\n"
    ds = _simple_diff(tmp_path, patch_content=small_patch)
    result = gen.render(sid, ds)
    assert "[Diff truncated." not in result.markdown


async def test_test_runner_events_appear_in_tests_section(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s6"
    await gen.handle(
        _make_event(
            sid,
            "tool.completed",
            {"tool": "pytest", "summary": "12 passed in 1.4s", "call_id": "c1"},
        )
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "pytest" in result.markdown
    assert "12 passed" in result.markdown


async def test_non_test_tool_not_in_tests_section(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s7"
    await gen.handle(
        _make_event(sid, "tool.completed", {"tool": "bash", "summary": "ok", "call_id": "c1"})
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "No test runner events recorded" in result.markdown


async def test_policy_events_appear_in_digest(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s8"
    await gen.handle(
        _make_event(
            sid,
            "policy.evaluated",
            {
                "call_id": "c1",
                "tool": "bash",
                "decision": "deny",
                "bucket": "specific_deny",
                "rationale": ".env access blocked",
            },
        )
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "deny" in result.markdown
    assert "specific_deny" in result.markdown
    assert ".env access blocked" in result.markdown


async def test_risk_flags_in_risk_notes(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s9"
    ds = DiffSummary(
        changed_files=[FileChange(path="src/auth.py", status="M")],
        risk_flags=[
            RiskFlag(
                category="auth_security",
                files=["src/auth.py"],
                note="Changes to authentication-related files detected.",
            )
        ],
    )
    result = gen.render(sid, ds)
    assert "auth_security" in result.markdown
    assert "src/auth.py" in result.markdown


async def test_suggested_review_checklist_for_risk_files(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s10"
    ds = DiffSummary(
        changed_files=[FileChange(path="src/auth.py", status="M")],
        risk_flags=[
            RiskFlag(
                category="auth_security",
                files=["src/auth.py"],
                note="auth change",
            )
        ],
    )
    result = gen.render(sid, ds)
    assert "- [ ] `src/auth.py`" in result.markdown


async def test_out_of_workspace_denials_add_risk_note(tmp_path: Path) -> None:
    """BL-25: workspace_containment denials add a blocked-access Risk Note."""
    gen = DigestGenerator(tmp_path)
    sid = "s-block"
    await gen.handle(
        _make_event(sid, "tool.denied", {"call_id": "c1", "reason": "workspace_containment", "path": "/etc/passwd"})
    )
    await gen.handle(
        _make_event(sid, "tool.denied", {"call_id": "c2", "reason": "workspace_containment", "path": "/outside/x.txt"})
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "2 access(es) blocked outside the workspace" in result.markdown


async def test_non_containment_denials_not_counted(tmp_path: Path) -> None:
    """BL-25 (CONCERN-A): secret and read-only-review denials carry real in-workspace
    paths but are NOT out-of-workspace breaches, so they add NO Risk Note."""
    gen = DigestGenerator(tmp_path)
    sid = "s-noblock"
    await gen.handle(
        _make_event(sid, "tool.denied", {"call_id": "c1", "reason": "deny_secrets", "path": SECRET_PATH_MASK})
    )
    await gen.handle(
        _make_event(sid, "tool.denied", {"call_id": "c2", "reason": "review_readonly", "path": "/ws/app.py"})
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "blocked outside the workspace" not in result.markdown


async def test_digest_md_written_to_disk(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s11"
    result = gen.render(sid, DiffSummary())
    assert Path(result.digest_path).exists()
    on_disk = Path(result.digest_path).read_text(encoding="utf-8")
    assert on_disk == result.markdown


async def test_digest_starts_with_heading(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    result = gen.render("s12", DiffSummary())
    assert result.markdown.startswith("#")


async def test_events_isolated_per_session(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    await gen.handle(_make_event("A", "session.started", {"task": "task-A"}))
    await gen.handle(_make_event("B", "session.started", {"task": "task-B"}))
    result_a = gen.render("A", DiffSummary())
    result_b = gen.render("B", DiffSummary())
    assert "task-A" in result_a.markdown
    md_b = result_b.markdown
    history_idx = md_b.find("## Session History")
    main_body_b = md_b[:history_idx] if history_idx != -1 else md_b
    assert "task-A" not in main_body_b
    assert "task-B" in result_b.markdown


async def test_untracked_files_in_changed_files_section(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s13"
    ds = DiffSummary(untracked_files=["brand_new.py"])
    result = gen.render(sid, ds)
    assert "brand_new.py" in result.markdown


async def test_deleted_files_in_changed_files_section(tmp_path: Path) -> None:
    gen = DigestGenerator(tmp_path)
    sid = "s14"
    ds = DiffSummary(deleted_files=[FileChange(path="old.py", status="D")])
    result = gen.render(sid, ds)
    assert "old.py" in result.markdown


async def test_per_session_digest_written(tmp_path: Path) -> None:
    """render writes latest digest.md AND a per-session digest-<sid>.md (BL-2)."""
    gen = DigestGenerator(tmp_path)
    sid = "sess-per-1"
    result = gen.render(sid, _simple_diff(tmp_path))
    assert (tmp_path / "digest.md").exists()  # latest preserved (non-breaking)
    per = tmp_path / f"digest-{sid}.md"
    assert per.exists()
    assert per.read_text(encoding="utf-8") == result.markdown


async def test_per_session_diff_patch_written(tmp_path: Path) -> None:
    """render archives the session's full patch as diff-<sid>.patch (BL-2)."""
    gen = DigestGenerator(tmp_path)
    sid = "sess-per-2"
    ds = _simple_diff(
        tmp_path, patch_content=b"diff --git a/foo.py b/foo.py\n+UNIQUE_SESSION_DIFF\n"
    )
    gen.render(sid, ds)
    per_patch = tmp_path / f"diff-{sid}.patch"
    assert per_patch.exists()
    assert b"UNIQUE_SESSION_DIFF" in per_patch.read_bytes()


async def test_per_session_history_survives_second_session(tmp_path: Path) -> None:
    """A second session must not clobber the first session's digest/patch history."""
    gen = DigestGenerator(tmp_path)
    await gen.handle(_make_event("A", "session.started", {"task": "task-A"}))
    gen.render("A", _simple_diff(tmp_path, patch_content=b"AAA_DIFF\n"))
    await gen.handle(_make_event("B", "session.started", {"task": "task-B"}))
    gen.render("B", _simple_diff(tmp_path, patch_content=b"BBB_DIFF\n"))

    assert "task-A" in (tmp_path / "digest-A.md").read_text(encoding="utf-8")
    assert "task-B" in (tmp_path / "digest-B.md").read_text(encoding="utf-8")
    assert b"AAA_DIFF" in (tmp_path / "diff-A.patch").read_bytes()
    assert b"BBB_DIFF" in (tmp_path / "diff-B.patch").read_bytes()
    assert "task-B" in (tmp_path / "digest.md").read_text(encoding="utf-8")  # latest


async def test_worker_report_section_from_session_response(tmp_path: Path) -> None:
    """A session.response event renders a labeled '## Worker Report' after Changed Files."""
    gen = DigestGenerator(tmp_path)
    sid = "wr-1"
    await gen.handle(
        _make_event(sid, "session.response", {"text": "I refactored the parser and added tests."})
    )
    result = gen.render(sid, _simple_diff(tmp_path))
    md = result.markdown
    assert "## Worker Report" in md
    assert "I refactored the parser and added tests." in md
    assert "not independently verified" in md.lower()
    assert md.index("## Worker Report") > md.index("## Changed Files")
    assert md.index("## Worker Report") < md.index("## Tests")


async def test_worker_report_omitted_when_no_response(tmp_path: Path) -> None:
    """No session.response event → no Worker Report section at all."""
    gen = DigestGenerator(tmp_path)
    sid = "wr-2"
    await gen.handle(_make_event(sid, "session.started", {"task": "x"}))
    result = gen.render(sid, _simple_diff(tmp_path))
    assert "## Worker Report" not in result.markdown


async def test_session_history_lists_prior_sessions(tmp_path: Path) -> None:
    """BL-11: Session History section lists prior sessions (not current), newest first."""
    import os

    gen = DigestGenerator(tmp_path)

    prior1 = tmp_path / "digest-prior-1.md"
    prior1.write_text(
        "# Session Digest\n\n"
        "- **Session ID:** `prior-1`\n"
        "- **Task:** build the thing\n"
        "- **Status:** completed\n"
        "- **Events:** 0\n",
        encoding="utf-8",
    )
    os.utime(prior1, (1_000_000.0, 1_000_000.0))

    prior2 = tmp_path / "digest-prior-2.md"
    prior2.write_text(
        "# Session Digest\n\n"
        "- **Session ID:** `prior-2`\n"
        "- **Task:** fix the bug\n"
        "- **Status:** completed\n"
        "- **Events:** 0\n",
        encoding="utf-8",
    )
    os.utime(prior2, (2_000_000.0, 2_000_000.0))

    result = gen.render("current-3", DiffSummary())
    md = result.markdown

    assert "## Session History" in md
    assert "- `prior-1` — build the thing ([digest-prior-1.md](digest-prior-1.md))" in md
    assert "- `prior-2` — fix the bug ([digest-prior-2.md](digest-prior-2.md))" in md

    history_start = md.index("## Session History")
    history_section = md[history_start:]
    assert history_section.index("prior-2") < history_section.index("prior-1")
    assert "current-3" not in history_section


async def test_session_history_absent_when_no_prior_sessions(tmp_path: Path) -> None:
    """BL-11: No prior digest files → no ## Session History section."""
    gen = DigestGenerator(tmp_path)
    result = gen.render("solo-session", DiffSummary())
    assert "## Session History" not in result.markdown


async def test_render_reads_events_from_disk(tmp_path: Path) -> None:
    import json
    import re
    from datetime import datetime, timezone

    sid = "disk-sess"
    ev = RuntimeEvent.model_validate({
        "id": 1,
        "session_id": sid,
        "type": "session.started",
        "timestamp_utc": datetime.now(timezone.utc),
        "payload": {"task": "optimise queries", "workspace": "/ws"},
    })
    (tmp_path / "events.jsonl").write_text(
        json.dumps(ev.model_dump(by_alias=True)) + "\n",
        encoding="utf-8",
    )
    gen = DigestGenerator(tmp_path)
    ds = DiffSummary(
        diff_stat=" db.py | 3 +++\n 1 file changed, 3 insertions(+)",
        changed_files=[FileChange(path="db.py", status="M")],
    )
    result = gen.render(sid, ds)
    md = result.markdown
    assert "optimise queries" in md
    assert "db.py" in md
    m = re.search(r"\*\*Events:\*\* (\d+)", md)
    assert m is not None and int(m.group(1)) > 0
