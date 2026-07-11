"""BL-20 — region capability probe policy (cache + verdict -> message mapping).

The live call (`_probe`) is monkeypatched here so these are hermetic; the real
Vertex round-trip is covered by live QA. RED before probe.py exists; GREEN after.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cao.runtime import probe, probe_cache
from cao.runtime.auth import AuthConfig


def _auth(*, mode: str = "vertex", location: str | None = "global", project: str | None = "proj") -> AuthConfig:
    return AuthConfig(mode=mode, model="gemini-3.5-flash", project=project, location=location, api_key=None)


async def test_api_key_mode_skips_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """gemini_api_key mode has no location -> probe is skipped entirely (returns None)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    called = False

    async def _fake(_auth_c: AuthConfig, _model: str) -> str:
        nonlocal called
        called = True
        return "unavailable"

    monkeypatch.setattr(probe, "_probe", _fake)
    result = await probe.check_region_available(_auth(mode="gemini_api_key", location=None), "gemini-3.5-flash")
    assert result is None
    assert called is False


async def test_unavailable_returns_recovery_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-servable region -> a recovery message naming the location (becomes -32602)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))

    async def _fake(_auth_c: AuthConfig, _model: str) -> str:
        return "unavailable"

    monkeypatch.setattr(probe, "_probe", _fake)
    msg = await probe.check_region_available(_auth(location="us-central1"), "gemini-3.5-flash")
    assert msg is not None
    assert "us-central1" in msg
    assert "gemini-3.5-flash" in msg


async def test_ok_caches_and_second_call_skips_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """First probe 'ok' -> cached; second call for same combo does NOT re-probe."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    calls = 0

    async def _fake(_auth_c: AuthConfig, _model: str) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    monkeypatch.setattr(probe, "_probe", _fake)
    auth = _auth()
    assert await probe.check_region_available(auth, "gemini-3.5-flash") is None
    assert await probe.check_region_available(auth, "gemini-3.5-flash") is None
    assert calls == 1
    assert probe_cache.is_ok("proj", "gemini-3.5-flash", "global")


def test_verdict_404_is_unavailable() -> None:
    """A 404/NOT_FOUND is the definitive 'region does not serve this model' signal."""

    class _Err(Exception):
        code = 404

    assert probe._verdict_for_error(_Err("404 NOT_FOUND. Publisher model ...")) == "unavailable"


def test_verdict_400_is_transient_not_unavailable() -> None:
    """400 is a catch-all (thinking model rejecting max_output_tokens=1, safety filter,
    malformed request) -> 'transient' (allow through), NOT a false 'unavailable' that
    would block a servable region."""

    class _Err(Exception):
        code = 400

    assert probe._verdict_for_error(_Err("400 INVALID_ARGUMENT max_output_tokens")) == "transient"


def test_verdict_5xx_is_transient() -> None:
    class _Err(Exception):
        code = 503

    assert probe._verdict_for_error(_Err("503 UNAVAILABLE")) == "transient"


def test_verdict_not_found_text_fallback() -> None:
    """No .code attr but the message says 'was not found' -> unavailable (backstop)."""
    assert probe._verdict_for_error(Exception("Publisher model X was not found")) == "unavailable"


async def test_transient_allows_through_and_not_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A transient error (timeout/5xx) allows the turn through and is NOT cached."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))

    async def _fake(_auth_c: AuthConfig, _model: str) -> str:
        return "transient"

    monkeypatch.setattr(probe, "_probe", _fake)
    auth = _auth(location="europe-west1")
    assert await probe.check_region_available(auth, "gemini-3.5-flash") is None
    assert not probe_cache.is_ok("proj", "gemini-3.5-flash", "europe-west1")
