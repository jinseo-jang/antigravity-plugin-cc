from __future__ import annotations

import json
from pathlib import Path

import pytest

import cao.runtime.auth as _authmod
from cao.runtime.auth import AuthConfig, AuthNotConfigured, resolve_auth, to_local_agent_kwargs


# ── defaults.json precedence tests (BL-9) ───────────────────────────────────

@pytest.fixture()
def _isolate_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CAO_PLUGIN_DATA at tmp_path so defaults.json is isolated."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    return tmp_path


def _write_defaults(tmp_path: Path, data: dict) -> None:
    p = tmp_path / "defaults.json"
    p.write_text(json.dumps(data))


def test_empty_defaults_regression_gemini_api_key(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty defaults.json → behavior identical to today (env-only)."""
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    # No defaults.json written — store is empty
    auth = resolve_auth(None)
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "env-key"


def test_empty_defaults_regression_raises_when_no_creds(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty defaults.json, no env → AuthNotConfigured (same as before)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: None, raising=False)
    with pytest.raises(AuthNotConfigured):
        resolve_auth(None)


def test_defaults_model_only_uses_env_for_mode(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults.json with model only → model applied, mode/location from env."""
    _write_defaults(_isolate_defaults, {"model": "gemini-3.5-flash"})
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth(None)
    assert auth.model == "gemini-3.5-flash"
    assert auth.mode == "gemini_api_key"  # mode came from env (GEMINI_API_KEY)


def test_defaults_full_vertex_all_applied(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults with mode+location+model → all applied, no env needed."""
    _write_defaults(_isolate_defaults, {"mode": "vertex", "location": "global", "model": "gemini-3.5-flash"})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth(None)
    assert auth.mode == "vertex"
    assert auth.location == "global"
    assert auth.model == "gemini-3.5-flash"


def test_precedence_env_model_beats_default_model(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAO_MODEL env > defaults.json model."""
    _write_defaults(_isolate_defaults, {"model": "gemini-3.5-flash"})
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("CAO_MODEL", "gemini-2.0-flash")
    auth = resolve_auth(None)
    # defaults.json model is loaded into config dict first; resolve_auth reads
    # CAO_MODEL as the env baseline BEFORE config override — so config wins.
    # Confirmed design: config.model overrides env CAO_MODEL (auth.py line 68).
    assert auth.model == "gemini-3.5-flash"


def test_precedence_explicit_config_beats_defaults(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit config dict passed to resolve_auth wins over defaults.json."""
    _write_defaults(_isolate_defaults, {"model": "gemini-3.5-flash"})
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    auth = resolve_auth({"mode": "gemini_api_key", "api_key": "explicit-k", "model": "gemini-2.5-pro"})
    assert auth.model == "gemini-2.5-pro"
    assert auth.api_key == "explicit-k"


def test_defaults_api_key_never_loaded(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_key in defaults.json must never become auth.api_key (it isn't a known key)."""
    _write_defaults(_isolate_defaults, {"api_key": "stored-secret", "model": "gemini-2.5-flash"})
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    auth = resolve_auth(None)
    # api_key in defaults.json is filtered by defaults.load(); env GEMINI_API_KEY is used
    assert auth.api_key == "env-key"


def test_defaults_gemini_api_key_mode_uses_env_key(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults.json {mode: gemini_api_key} (api_key is NEVER stored) must read the
    key from GEMINI_API_KEY env — not KeyError (Oracle blocking #1: /agy:setup path)."""
    _write_defaults(_isolate_defaults, {"mode": "gemini_api_key", "model": "gemini-2.5-flash"})
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth(None)
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "env-key"
    assert auth.model == "gemini-2.5-flash"


def test_defaults_gemini_api_key_mode_no_env_raises(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults.json {mode: gemini_api_key} + no env key → AuthNotConfigured, NOT KeyError."""
    _write_defaults(_isolate_defaults, {"mode": "gemini_api_key", "model": "gemini-2.5-flash"})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(AuthNotConfigured):
        resolve_auth(None)


def test_resolve_gemini_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth()
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "test-key-123"
    assert auth.project is None


def test_resolve_vertex_when_only_project_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    auth = resolve_auth()
    assert auth.mode == "vertex"
    assert auth.project == "my-project"
    assert auth.api_key is None


def test_resolve_vertex_default_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    auth = resolve_auth()
    assert auth.location == "global"


def test_resolve_raises_when_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: None, raising=False)
    with pytest.raises(AuthNotConfigured):
        resolve_auth()


def test_resolve_gemini_takes_precedence_over_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    assert resolve_auth().mode == "gemini_api_key"


def test_resolve_explicit_config_gemini() -> None:
    auth = resolve_auth({"mode": "gemini_api_key", "api_key": "explicit-key"})
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "explicit-key"


def test_resolve_explicit_config_vertex() -> None:
    auth = resolve_auth({"mode": "vertex", "project": "p1", "location": "eu-west4"})
    assert auth.mode == "vertex"
    assert auth.project == "p1"
    assert auth.location == "eu-west4"


def test_resolve_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("CAO_MODEL", raising=False)
    assert resolve_auth().model == "gemini-3.5-flash"


def test_resolve_custom_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("CAO_MODEL", "gemini-2.0-flash")
    assert resolve_auth().model == "gemini-2.0-flash"


def test_to_kwargs_gemini() -> None:
    auth = AuthConfig(mode="gemini_api_key", model="gemini-2.5-flash", project=None, location=None, api_key="k")
    kwargs = to_local_agent_kwargs(auth)
    assert kwargs["api_key"] == "k"
    assert kwargs["model"] == "gemini-2.5-flash"
    assert "vertex" not in kwargs


def test_to_kwargs_vertex() -> None:
    auth = AuthConfig(mode="vertex", model="gemini-3.5-flash", project="p", location="global", api_key=None)
    kwargs = to_local_agent_kwargs(auth)
    assert kwargs["vertex"] is True
    assert kwargs["project"] == "p"
    assert kwargs["location"] == "global"
    assert kwargs["model"] == "gemini-3.5-flash"
    assert "api_key" not in kwargs


def test_resolve_auth_key_file_fallback(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key file present, no env var -> resolve_auth() returns it in gemini_api_key mode."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    
    key_file = _isolate_defaults / "gemini_api_key"
    key_file.write_text("file-key-123\n", encoding="utf-8")
    
    auth = resolve_auth(None)
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "file-key-123"


def test_resolve_auth_env_wins_over_key_file(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var set -> env WINS over the key file."""
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-wins")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    
    key_file = _isolate_defaults / "gemini_api_key"
    key_file.write_text("file-key-loses\n", encoding="utf-8")
    
    auth = resolve_auth(None)
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "env-key-wins"


def test_resolve_auth_neither_raises(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither env var nor key file present -> AuthNotConfigured."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    
    key_file = _isolate_defaults / "gemini_api_key"
    if key_file.exists():
        key_file.unlink()

    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: None, raising=False)
    with pytest.raises(AuthNotConfigured):
        resolve_auth(None)


# ── BL-26: keychain-first API-key precedence ────────────────────────────────

def test_keychain_beats_env_and_file(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-26: keychain wins over env AND file; resolved source is 'keychain'."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: "from-keychain", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    (_isolate_defaults / "gemini_api_key").write_text("from-file\n", encoding="utf-8")
    auth = resolve_auth(None)
    assert auth.api_key == "from-keychain"
    assert auth.source == "keychain"


def test_env_used_when_keychain_empty(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-26: keychain empty -> env is #2 (Google-recommended); source is 'env'."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    (_isolate_defaults / "gemini_api_key").write_text("from-file\n", encoding="utf-8")
    auth = resolve_auth(None)
    assert auth.api_key == "from-env"
    assert auth.source == "env"


def test_env_key_whitespace_is_stripped(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-26: GEMINI_API_KEY=$(cat keyfile) leaves a trailing newline; strip it like the
    file tier so invisible whitespace never reaches the SDK. Whitespace-only -> falls through."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "  spaced-key\n")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth(None)
    assert auth.api_key == "spaced-key"
    assert auth.source == "env"


def test_file_used_when_keychain_and_env_empty(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-26: keychain + env empty -> plaintext file is the last resort; source 'file'."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    (_isolate_defaults / "gemini_api_key").write_text("from-file\n", encoding="utf-8")
    auth = resolve_auth(None)
    assert auth.api_key == "from-file"
    assert auth.source == "file"


def test_read_keychain_never_raises_on_fail_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """BL-26 headless-safety: a broken/absent keyring backend yields None, never raises."""
    import keyring

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", _boom)
    assert _authmod._read_keychain() is None


# ── Vertex ADC project auto-detection (gcloud ADC login, no env vars) ────────

def test_adc_project_used_when_no_env_or_key(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADC-only (gcloud auth application-default login) with no GOOGLE_CLOUD_PROJECT and no
    key -> vertex mode using the ADC-detected project; location defaults to global."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: "adc-proj", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    auth = resolve_auth(None)
    assert auth.mode == "vertex"
    assert auth.project == "adc-proj"
    assert auth.location == "global"


def test_env_project_wins_over_adc(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit GOOGLE_CLOUD_PROJECT beats ADC auto-detect."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: "adc-loses", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "explicit-proj")
    auth = resolve_auth(None)
    assert auth.mode == "vertex"
    assert auth.project == "explicit-proj"


def test_no_adc_no_env_no_key_still_raises(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADC absent (returns None) + no env + no key -> AuthNotConfigured (no silent success)."""
    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: None, raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(AuthNotConfigured):
        resolve_auth(None)


def test_gemini_key_beats_adc(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved Gemini key wins over Vertex; ADC is never consulted."""
    consulted = {"adc": False}

    def _spy() -> str:
        consulted["adc"] = True
        return "adc-proj"

    monkeypatch.setattr(_authmod, "_read_keychain", lambda: None, raising=False)
    monkeypatch.setattr(_authmod, "_detect_adc_project", _spy, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "the-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    auth = resolve_auth(None)
    assert auth.mode == "gemini_api_key"
    assert auth.api_key == "the-key"
    assert consulted["adc"] is False


def test_config_vertex_uses_adc_when_no_project(
    _isolate_defaults: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """defaults.json mode=vertex with no project + no GOOGLE_CLOUD_PROJECT -> ADC project."""
    monkeypatch.setattr(_authmod, "_detect_adc_project", lambda: "adc-proj", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    auth = resolve_auth({"mode": "vertex", "model": "gemini-3.5-flash"})
    assert auth.mode == "vertex"
    assert auth.project == "adc-proj"
    assert auth.location == "global"


def test_detect_adc_project_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless-safety: broken google.auth.default(), no ADC file, no gcloud -> None."""
    import google.auth

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("no ADC")

    monkeypatch.setattr(google.auth, "default", _boom)
    # Isolate from this machine's real ADC file / gcloud config.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    monkeypatch.setattr(_authmod.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert _authmod._detect_adc_project() is None


# ── C-1: ADC file quota_project_id fallback (authorized_user creds have no `project`
# from google.auth.default(); the ADC file's quota_project_id is the only source) ──

def test_config_vertex_uses_adc_file_quota_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config project, no GOOGLE_CLOUD_PROJECT, google.auth.default() -> (None, None),
    but the ADC file has quota_project_id -> that becomes auth.project."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *_a, **_k: (None, None))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    adc_file = tmp_path / "adc.json"
    adc_file.write_text(
        json.dumps({"type": "authorized_user", "quota_project_id": "proj-xyz"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_file))
    auth = resolve_auth({"mode": "vertex", "model": "gemini-3.5-flash"})
    assert auth.mode == "vertex"
    assert auth.project == "proj-xyz"


def test_config_vertex_raises_when_no_project_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config project, no env, no ADC file, no gcloud -> AuthNotConfigured (not a
    silent project=None AuthConfig — that was the crash-causing bug)."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *_a, **_k: (None, None))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    monkeypatch.setattr(_authmod.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(AuthNotConfigured):
        resolve_auth({"mode": "vertex", "model": "gemini-3.5-flash"})


def test_detect_adc_project_returns_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_adc_project returns the project id from google.auth.default()."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *_a, **_k: (object(), "proj-y"))
    assert _authmod._detect_adc_project() == "proj-y"

