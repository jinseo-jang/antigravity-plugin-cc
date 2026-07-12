"""Global test isolation.

Hermeticity: several tests call ``resolve_auth(None)`` / the approval store without
isolating ``CAO_PLUGIN_DATA``, so they would read the developer's REAL
``~/.config/cao/{defaults.json,approvals.json}``. A developer who has run
``/agy:setup`` (or approved a command globally) would then get spurious failures.
Point every test at a throwaway data dir so the suite never reads real user config.
Tests that specifically exercise the ``~/.config/cao`` fallback ``delenv`` it themselves.
"""
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_cao_plugin_data(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path_factory.mktemp("cao_data")))


@pytest.fixture(autouse=True)
def _stub_region_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermeticity: the BL-20 capability probe makes a live Vertex call. Stub the live
    part (probe._probe) to 'ok' so no unit test hits the network; tests exercising probe
    behavior override probe._probe or daemon.check_region_available themselves."""
    from cao.runtime import probe

    async def _ok(_auth: object, _model: str) -> str:
        return "ok"

    monkeypatch.setattr(probe, "_probe", _ok)


@pytest.fixture(autouse=True)
def _stub_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermeticity: resolve_auth reads the OS keychain (BL-26). Stub keyring.get_password
    to None so no unit test reads the developer's real keychain; keychain tests override
    _read_keychain (or keyring.get_password) themselves. The null backend env var (inherited
    by subprocess companion tests) stops a broken host D-Bus backend from stalling."""
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _stub_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermeticity: resolve_auth falls back to gcloud ADC for the Vertex project. Stub
    google.auth.default to yield no project so the suite never reads the developer's real
    ADC; ADC tests override _detect_adc_project (or google.auth.default) themselves."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *_a, **_k: (None, None))


@pytest.fixture(autouse=True)
def _clear_ambient_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermeticity: never let a maintainer's real GOOGLE_CLOUD_PROJECT / GEMINI_API_KEY
    leak into resolve_auth tests. Tests that need them set them explicitly."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_gcp_project(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Hermeticity: this dev box has real ADC/gcloud resolving a GCP project, which masks
    CI (which has none). Neutralize all three sources resolve_auth's _detect_adc_project reads,
    so tests see 'no project' by default like CI. Tests that need a project set it explicitly
    (config 'project', GOOGLE_CLOUD_PROJECT, or monkeypatching _detect_adc_project) AFTER this
    fixture runs, so their override wins. Tests of _detect_adc_project re-patch google.auth /
    GOOGLE_APPLICATION_CREDENTIALS in their own body, which also overrides this."""
    try:
        import google.auth  # noqa: PLC0415

        monkeypatch.setattr(google.auth, "default", lambda *a, **k: (None, None))
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path_factory.mktemp("no_adc") / "nope.json"))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path_factory.mktemp("no_gcloud")))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)

    # ponytail: this box is a GCE-backed dev workstation with a live metadata server, so
    # `gcloud config get-value project` falls back to it and returns a real project even
    # with CLOUDSDK_CONFIG pointed at an empty dir (CI has neither gcloud nor metadata).
    # Block only the gcloud argv tier-3 uses; every other subprocess.run call (git, bash
    # hooks, companion tests) passes through untouched.
    import subprocess as _subprocess  # noqa: PLC0415

    _real_run = _subprocess.run

    def _no_gcloud(cmd: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "gcloud":
            raise OSError("gcloud disabled for hermetic tests")
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _no_gcloud)
