"""Read a Claude Code ``.jsonl`` transcript and build a bounded handoff summary.

Stdlib only (``json`` + ``pathlib`` + ``logging``); Pydantic v2 for the turn
model. No LLM, no network, no new dependency — the summary is pure truncation
(see :func:`summarize`). This module is the whole handoff mechanism for
``/agy:handoff`` (ADR-0005: text-context transfer via ``system_instructions``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger("cao.transcript")


class TranscriptTurn(BaseModel):
    """One salient conversation turn extracted from a Claude .jsonl transcript."""

    role: str  # "user" | "assistant"
    text: str


def read_turns(transcript_path: str | Path) -> list[TranscriptTurn]:
    """Read a Claude ``.jsonl`` transcript into ordered user/assistant text turns.

    Keeps only lines whose top-level ``type`` is ``"user"`` or ``"assistant"``
    and that carry a ``message`` object; every other record type (``summary``,
    ``system``, meta/sidechain) is skipped silently. ``message["content"]`` may
    be a ``str`` (used directly) or a list of blocks, in which case only the
    ``text`` of ``type == "text"`` blocks is concatenated (joined by newlines),
    ignoring ``thinking``/``tool_use``/``tool_result``/``image`` blocks. A turn
    with empty extracted text is dropped. A malformed line is skipped (not
    fatal); a missing/unreadable file returns ``[]`` (does not raise). This is
    the pure parser — no summarization or bounding happens here.
    """
    path = Path(transcript_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("transcript unreadable at %s: %s", path, exc)
        return []

    turns: list[TranscriptTurn] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            logger.debug("skipping malformed transcript line: %.80r", line)
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or obj.get("type"))
        text = _extract_text(message.get("content"))
        if text:
            turns.append(TranscriptTurn(role=role, text=text))
    return turns


def _extract_text(content: object) -> str:
    """Extract plain text from a message ``content`` (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text:
                    texts.append(block_text)
        return "\n".join(texts).strip()
    return ""


# ponytail: naive last-N-turns + char-cap truncation, NO semantic/LLM summarization.
# Ceiling: recent turns win; a very long single turn is hard-truncated; older turns
# drop first. Upgrade path: replace summarize()'s body with an extractive/LLM
# summarizer if fidelity ever matters — signature and callers stay the same.
_MAX_TURNS: int = 40          # keep the last N turns (recency = relevance for continuation)
_MAX_TURN_CHARS: int = 1000   # per-turn hard cap
_MAX_CHARS: int = 12_000      # total hard cap, well under the harness compaction threshold

_EARLIER_MARKER: str = "…[earlier turns omitted]\n\n"


def summarize(
    turns: list[TranscriptTurn],
    *,
    max_turns: int = _MAX_TURNS,
    max_chars: int = _MAX_CHARS,
    max_turn_chars: int = _MAX_TURN_CHARS,
) -> str:
    """Render turns into a hard-bounded plain-text summary (no LLM).

    Keeps the last ``max_turns`` turns, renders each as ``"{ROLE}: {text}"`` with
    ``text`` truncated to ``max_turn_chars`` (``" …[truncated]"`` appended when
    cut), joins with blank lines, then applies a final tail guard: if the joined
    string exceeds ``max_chars`` the leading (oldest) characters are dropped and
    a single ``"…[earlier turns omitted]\\n\\n"`` marker is prefixed. The returned
    string length is therefore ``<= max_chars + len(marker)``. Empty input
    returns ``""``. The caps are the token-blowup boundary and are enforced here,
    never by the caller.
    """
    if not turns:
        return ""

    rendered: list[str] = []
    for turn in turns[-max_turns:]:
        text = turn.text
        if len(text) > max_turn_chars:
            text = text[:max_turn_chars] + " …[truncated]"
        rendered.append(f"{turn.role.upper()}: {text}")

    joined = "\n\n".join(rendered)
    if len(joined) > max_chars:
        joined = _EARLIER_MARKER + joined[-max_chars:]
    return joined


def load_handoff_summary(transcript_path: str | None) -> str | None:
    """Turn a transcript path into a bounded summary, or ``None`` if unusable.

    Returns ``None`` for a ``None``/empty path, a missing file, or a transcript
    that yields no turns (or an empty summary). ``None`` — not ``""`` — is the
    signal the daemon/``build_agent_config`` seam uses to decide whether to
    attach ``system_instructions`` at all, keeping byte-identical behavior when
    there is no usable transcript.
    """
    if not transcript_path:
        return None
    turns = read_turns(transcript_path)
    if not turns:
        return None
    summary = summarize(turns)
    return summary or None


HANDOFF_PREAMBLE: str = (
    "You are continuing work handed off from a prior Claude Code conversation. "
    "The following is a bounded summary of that conversation, provided for context. "
    "Treat it as background; follow the task instruction you are given."
)


def build_handoff_instructions(summary: str, base: str | None = None) -> str:
    """Compose worker instructions as: [base] + preamble + summary (never replacing base)."""
    parts = [p for p in (base, HANDOFF_PREAMBLE, summary) if p]
    return "\n\n".join(parts)
