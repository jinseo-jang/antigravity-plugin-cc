"""Unit tests for cao.runtime.multimodal.resolve_attachments (Task 008 AC1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cao.runtime.multimodal import AttachmentError, resolve_attachments

# A minimal 1x1 PNG. Content is irrelevant to validation (MIME is derived from
# the .png extension), but a real header keeps the fixture honest.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def test_accepts_in_workspace_png(tmp_path: Path) -> None:
    png = tmp_path / "pic.png"
    png.write_bytes(_PNG_BYTES)
    parts = resolve_attachments([str(png)], str(tmp_path))
    assert len(parts) == 1
    assert parts[0].mime_type == "image/png"


def test_rejects_out_of_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    with pytest.raises(AttachmentError):
        resolve_attachments([str(outside)], str(ws))


def test_rejects_secret_path(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc")
    with pytest.raises(AttachmentError):
        resolve_attachments([str(secret)], str(tmp_path))


def test_rejects_unsupported_extension(tmp_path: Path) -> None:
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")
    with pytest.raises(AttachmentError):
        resolve_attachments([str(exe)], str(tmp_path))


def test_empty_input_returns_empty(tmp_path: Path) -> None:
    assert resolve_attachments([], str(tmp_path)) == []
