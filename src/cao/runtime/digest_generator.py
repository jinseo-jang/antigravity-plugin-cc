"""DigestGenerator: renders a compact Markdown digest from events + DiffSummary.

digest_generator.md §4.3 / task 003 §4.3 implementation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cao.models import Digest, DiffSummary, RuntimeEvent

logger = logging.getLogger("cao.digest")

_DIFF_TRUNCATE_BYTES = 8192

_TEST_TOOLS = ("pytest", "jest", "cargo test", "go test")


class DigestGenerator:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._events: dict[str, list[RuntimeEvent]] = {}

    async def handle(self, event: RuntimeEvent) -> None:
        self._events.setdefault(event.session_id, []).append(event)

    def _load_events(self, session_id: str) -> list[RuntimeEvent]:
        path = self._state_dir / "events.jsonl"
        if path.exists():
            events: list[RuntimeEvent] = []
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    if data.get("session_id") == session_id:
                        events.append(RuntimeEvent.model_validate(data))
                except Exception:
                    pass
            if events:
                return events
        return self._events.get(session_id, [])

    def _collect_history(self, session_id: str) -> list[tuple[str, str]]:
        # ponytail: line scan over fixed digest header format; upgrade path: sidecar index if format changes
        entries: list[tuple[float, str, str]] = []
        for path in self._state_dir.glob("digest-*.md"):
            if path.name == f"digest-{session_id}.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            sid, task = _parse_digest_header(text)
            if sid:
                entries.append((mtime, sid, task))
        entries.sort(key=lambda e: e[0], reverse=True)
        return [(sid, task) for _, sid, task in entries]

    def render(self, session_id: str, diff_summary: DiffSummary) -> Digest:
        events = self._load_events(session_id)
        history = self._collect_history(session_id)
        md = _build_markdown(session_id, events, diff_summary, history)
        digest_path = self._state_dir / "digest.md"
        # ponytail: small file, sync write acceptable
        digest_path.write_text(md, encoding="utf-8")
        # BL-2: digest.md stays "latest"; per-session copies are the durable
        # history. session_id is an internal UUID (not a trust boundary), so it is
        # safe to put in a filename without sanitisation.
        (self._state_dir / f"digest-{session_id}.md").write_text(md, encoding="utf-8")
        if diff_summary.patch_path:
            src = Path(diff_summary.patch_path)
            if src.exists():
                (self._state_dir / f"diff-{session_id}.patch").write_bytes(src.read_bytes())
        logger.debug("digest written (latest + digest-%s.md) to %s", session_id, self._state_dir)
        return Digest(markdown=md, digest_path=str(digest_path))


def _parse_digest_header(text: str) -> tuple[str, str]:
    sid = ""
    task = ""
    for line in text.splitlines():
        if not sid and line.startswith("- **Session ID:** `"):
            sid = line.removeprefix("- **Session ID:** `").removesuffix("`")
        elif not task and line.startswith("- **Task:** "):
            task = line.removeprefix("- **Task:** ")
        if sid and task:
            break
    return sid, task


def _build_markdown(
    session_id: str,
    events: list[RuntimeEvent],
    diff: DiffSummary,
    history: list[tuple[str, str]] | None = None,
) -> str:
    parts: list[str] = []

    # 1. Summary
    task_desc = ""
    final_status = "unknown"
    model_line = ""
    for e in events:
        if e.event_type == "session.started":
            task_desc = str(e.payload.get("task", ""))
        if e.event_type == "session.ended":
            final_status = str(e.payload.get("status", "completed"))
        if e.event_type == "session.model":
            model_line = (
                f"\n- **Model / effort:** `{e.payload.get('model', '?')}`"
                f" / {e.payload.get('effort', 'default')}"
            )
    parts.append(
        "# Session Digest\n\n"
        f"- **Session ID:** `{session_id}`\n"
        f"- **Task:** {task_desc or '(not recorded)'}\n"
        f"- **Status:** {final_status}\n"
        f"- **Events:** {len(events)}"
        f"{model_line}"
    )

    # 2. Changed files
    cf: list[str] = ["## Changed Files"]
    if diff.no_git_repo:
        cf.append("*(git not available — see Risk Notes)*")
    else:
        if diff.diff_stat:
            cf.append(f"\n```\n{diff.diff_stat.rstrip()}\n```")
        if diff.changed_files:
            cf.append("\n**Modified / Added:**")
            for fc in diff.changed_files:
                cf.append(f"- `{fc.status}` {fc.path}")
        if diff.untracked_files:
            cf.append("\n**Untracked (new):**")
            for f in diff.untracked_files:
                cf.append(f"- {f}")
        if diff.deleted_files:
            cf.append("\n**Deleted:**")
            for fc in diff.deleted_files:
                cf.append(f"- `{fc.status}` {fc.path}")
        if not diff.changed_files and not diff.untracked_files and not diff.deleted_files:
            cf.append("No file changes detected.")
        if diff.patch_path:
            try:
                raw = Path(diff.patch_path).read_bytes()
                if len(raw) > _DIFF_TRUNCATE_BYTES:
                    snippet = raw[:_DIFF_TRUNCATE_BYTES].decode("utf-8", errors="replace")
                    cf.append(f"\n```diff\n{snippet}\n```")
                    cf.append(
                        f"[Diff truncated. Full patch at: {diff.patch_path}]"
                    )
                elif raw:
                    cf.append(
                        f"\n```diff\n{raw.decode('utf-8', errors='replace')}\n```"
                    )
            except OSError:
                pass
    parts.append("\n".join(cf))

    # 2b. Worker Report (BL-4): the agent's own narration. Placed after Changed
    # Files and explicitly labeled non-objective — git diff is the authority.
    worker_text = ""
    for e in events:
        if e.event_type == "session.response":
            worker_text = str(e.payload.get("text", ""))
    if worker_text:
        # Blockquote every line so worker-emitted "## ..." cannot spoof a digest section.
        quoted = "\n".join(f"> {ln}" for ln in worker_text.splitlines()) or "> (empty)"
        parts.append(
            "## Worker Report\n"
            "> Agent's own summary — not independently verified;"
            " Changed Files is the objective record.\n>\n"
            f"{quoted}"
        )

    # 3. Tests
    test_events = [
        e for e in events
        if e.event_type == "tool.completed"
        and any(kw in str(e.payload.get("tool", "")).lower() for kw in _TEST_TOOLS)
    ]
    ts: list[str] = ["## Tests"]
    if test_events:
        for e in test_events:
            ts.append(
                f"- **{e.payload.get('tool', '?')}:** {e.payload.get('summary', '')}"
            )
    else:
        ts.append("No test runner events recorded.")
    parts.append("\n".join(ts))

    # 4. Policy decisions
    _DECISION_TYPES = frozenset({"policy.evaluated", "tool.approved", "tool.denied", "tool.auto_allowed"})
    decision_events = [e for e in events if e.event_type in _DECISION_TYPES]
    ps: list[str] = ["## Policy Decisions"]
    if decision_events:
        for e in decision_events:
            call_id_str = str(e.payload.get("call_id", "?"))
            if e.event_type == "tool.approved":
                ps.append(f"- **{call_id_str}** → approved")
            elif e.event_type == "tool.denied":
                reason = e.payload.get("reason", "")
                suffix = " (timeout)" if reason == "timeout" else ""
                ps.append(f"- **{call_id_str}** → denied{suffix}")
            elif e.event_type == "tool.auto_allowed":
                tool_n = str(e.payload.get("tool_name", call_id_str))
                ps.append(f"- **{tool_n}** → auto-allowed")
            else:
                tool = str(e.payload.get("tool", e.payload.get("call_id", "?")))
                ps.append(
                    f"- **{tool}** → {e.payload.get('decision', '?')}"
                    f" (bucket: {e.payload.get('bucket', '?')})"
                    f": {e.payload.get('rationale', '')}"
                )
    else:
        ps.append("No policy evaluation events recorded.")
    parts.append("\n".join(ps))

    # 5. Risk notes
    rs: list[str] = ["## Risk Notes"]
    # BL-25: count ONLY workspace-containment denials (the hook tags each deny by
    # policy). Secret (deny_secrets) and read-only-review (review_readonly) denials
    # carry real in-workspace paths but are NOT out-of-workspace breaches — excluded.
    blocked = sum(
        1
        for e in events
        if e.event_type == "tool.denied"
        and e.payload.get("reason") == "workspace_containment"
    )
    if diff.no_git_repo:
        rs.append(
            "> ⚠️ **Objective filesystem verification is unavailable:"
            " the git binary is not available."
            " Changes cannot be independently verified.**"
        )
    if blocked:
        rs.append(f"- **Containment:** {blocked} access(es) blocked outside the workspace")
    if diff.risk_flags:
        for rf in diff.risk_flags:
            files_str = ", ".join(f"`{f}`" for f in rf.files)
            rs.append(f"- **{rf.category}:** {rf.note} ({files_str})")
    elif not diff.no_git_repo and not blocked:
        rs.append("No risk flags detected.")
    parts.append("\n".join(rs))

    # 6. Suggested Claude review
    flagged: set[str] = {f for rf in diff.risk_flags for f in rf.files}
    review_files = [fc.path for fc in diff.changed_files if fc.path in flagged]
    rv: list[str] = ["## Suggested Claude Review"]
    if review_files:
        for p in review_files:
            rv.append(f"- [ ] `{p}`")
    else:
        rv.append("No files require priority review.")
    parts.append("\n".join(rv))

    # 7. Session History (BL-11)
    if history:
        sh: list[str] = ["## Session History"]
        for prior_sid, task in history:
            sh.append(
                f"- `{prior_sid}` — {task}"
                f" ([digest-{prior_sid}.md](digest-{prior_sid}.md))"
            )
        parts.append("\n".join(sh))

    return "\n\n".join(parts)
