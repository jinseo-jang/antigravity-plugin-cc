"""Supported-model allowlist (region-agnostic).

`check_model` returns a recovery-worded -32602 message when the model is not in
the supported set, else None. Region is the user's choice and is NOT validated
here — Gemini's regional availability keeps expanding. Pure function; the
allowlist is hardcoded.
"""
from __future__ import annotations

from cao.runtime.compat import check_model


# --- happy paths: supported models → None ------------------------------------


def test_flash_supported() -> None:
    """Given gemini-3.5-flash, When checked, Then None (supported)."""
    assert check_model("gemini-3.5-flash") is None


def test_pro_preview_supported() -> None:
    """Given gemini-3.1-pro-preview, When checked, Then None (supported)."""
    assert check_model("gemini-3.1-pro-preview") is None


def test_gemini36_flash_supported() -> None:
    """Given gemini-3.6-flash (new default), When checked, Then None (supported)."""
    assert check_model("gemini-3.6-flash") is None


def test_gemini35_flash_lite_supported() -> None:
    """Given gemini-3.5-flash-lite, When checked, Then None (supported)."""
    assert check_model("gemini-3.5-flash-lite") is None


# --- regression: unsupported models are rejected -----------------------------


def test_gemini25_flash_rejected() -> None:
    """gemini-2.5-flash is not in the allowlist → Unsupported model message."""
    msg = check_model("gemini-2.5-flash")
    assert msg is not None
    assert "Unsupported model" in msg
    assert "gemini-2.5-flash" in msg
    assert "gemini-3.5-flash" in msg  # names a fix


def test_bare_gemini31_pro_rejected() -> None:
    """bare gemini-3.1-pro (no -preview) is HTTP 404 → must be rejected."""
    msg = check_model("gemini-3.1-pro")
    assert msg is not None
    assert "Unsupported model" in msg
    assert "gemini-3.1-pro" in msg


def test_none_model_rejected() -> None:
    """None model resolves to '' which is not in the allowlist."""
    msg = check_model(None)
    assert msg is not None
    assert "Unsupported model" in msg
