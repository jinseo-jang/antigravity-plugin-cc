"""Resolve `--file` attachments into SDK media parts, validating each path.

One public function (:func:`resolve_attachments`) and one exception
(:class:`AttachmentError`). No new secret/workspace logic: containment reuses the
session's ``workspace`` string and the secret deny reuses
``policy_engine._is_credential_path``. The supported-MIME set is the union of the
SDK's four public frozensets, computed at import, so a format the SDK adds is
picked up automatically.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from google.antigravity import types  # type: ignore[import-untyped]

from cao.runtime.policy_engine import _is_credential_path

logger = logging.getLogger("cao.multimodal")

# Supported-MIME source of truth: the union of the SDK's four public frozensets.
# Never hardcode a MIME/extension list — if the SDK adds a format, this follows.
_SUPPORTED_MIMES: frozenset[str] = (
    types.SUPPORTED_IMAGE_MIMES
    | types.SUPPORTED_DOCUMENT_MIMES
    | types.SUPPORTED_AUDIO_MIMES
    | types.SUPPORTED_VIDEO_MIMES
)


class AttachmentError(Exception):
    """A ``--file`` path failed validation (missing, out-of-workspace, secret, or
    unsupported type). Carries a human-readable, path-specific message."""


def resolve_attachments(paths: list[str], workspace_root: str) -> list[Any]:
    """Validate each path and return the SDK media parts, in input order.

    Each returned element is an SDK Image | Document | Audio | Video built by
    ``types.from_file``. Raises :class:`AttachmentError` on the first path that
    fails any check. An empty/absent input returns ``[]``. No bytes are read
    until a path has passed every non-byte check.

    Validation order, short-circuiting on the first failure: exists (regular
    file) -> inside ``workspace_root`` -> not a secret path -> supported MIME ->
    ``types.from_file`` (the only step that reads bytes).
    """
    if not paths:
        return []
    root = Path(workspace_root).expanduser().resolve()
    parts: list[Any] = []
    for p in paths:
        abs_path = Path(p).expanduser().resolve()
        if not abs_path.is_file():
            raise AttachmentError(f"Attachment not found or not a regular file: {p}")
        if not abs_path.is_relative_to(root):
            raise AttachmentError(f"Attachment is outside the workspace: {p}")
        tc = types.ToolCall(name="attachment", canonical_path=str(abs_path))
        if _is_credential_path(tc):
            raise AttachmentError(f"Attachment is a denied secret file: {p}")
        mime = mimetypes.guess_type(abs_path)[0]
        if mime is None or mime not in _SUPPORTED_MIMES:
            raise AttachmentError(
                f"Attachment type is unsupported ({mime or 'unknown'}): {p}"
            )
        # ponytail: reads the whole file into memory (SDK from_file does likewise);
        # fine for review/implement attachment sizes; stream if huge media becomes a use case.
        parts.append(types.from_file(abs_path))
    logger.debug("resolved %d attachment(s) under %s", len(parts), root)
    return parts
