"""Fail-fast supported-model allowlist.

``check_model`` returns an immediate, Claude-renderable recovery message for
any model outside the two supported codes, preventing a silent ~600s worker
hang. Region is deliberately NOT restricted here: Gemini's regional
availability keeps expanding, so the user picks the location; an unavailable
model×region surfaces via the worker-turn timeout, not a hardcoded gate.

# ponytail: allowlist hardcoded (two models). Upgrade path: read from config
# or a live REST capability probe cached per (model, location).
"""

from __future__ import annotations

_SUPPORTED: frozenset[str] = frozenset({"gemini-3.5-flash", "gemini-3.1-pro-preview"})


def check_model(model: str | None) -> str | None:
    effective = model or ""
    if effective not in _SUPPORTED:
        return (
            f"Unsupported model '{effective}'. agy supports only gemini-3.5-flash (GA)"
            " or gemini-3.1-pro-preview (preview)."
            " Options: re-run with --model gemini-3.5-flash, or set a default via /agy:setup."
        )
    return None
