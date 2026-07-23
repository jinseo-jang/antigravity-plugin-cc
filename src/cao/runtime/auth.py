"""User-facing auth-mode config surface: Vertex ADC or Gemini API key."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from google.antigravity.models import (  # type: ignore[import-untyped]
    GeminiAPIEndpoint,
    GeminiModelOptions,
    ModelTarget,
    ThinkingLevel,
    VertexEndpoint,
)

from cao.runtime import defaults

_DEFAULT_MODEL = "gemini-3.6-flash"
_DEFAULT_LOCATION = "global"


class AuthNotConfigured(Exception):
    """No credential path is configured.

    Set one of:
      • gcloud ADC — Vertex AI: ``gcloud auth application-default login`` with an
        active project (``gcloud config set project``); GOOGLE_CLOUD_PROJECT optional
      • GEMINI_API_KEY — Gemini API (api_key= mode)

    Or run ``/agy:setup`` to write defaults.json
    (``$CAO_PLUGIN_DATA/defaults.json`` or ``~/.config/cao/defaults.json``).
    """

    def __init__(self) -> None:
        super().__init__(
            "No credentials configured. "
            "For Vertex AI, run `gcloud auth application-default login` (with an active "
            "project via `gcloud config set project`) - no env vars needed. "
            "For the Gemini API, set GEMINI_API_KEY. "
            "Or run /agy:setup to persist defaults.json."
        )


@dataclass
class AuthConfig:
    mode: Literal["vertex", "gemini_api_key"]
    model: str
    project: str | None
    location: str | None
    api_key: str | None
    # Which tier the api_key came from (BL-26 precedence proof). Never the key itself,
    # so it is safe to log. None for vertex mode (no api_key).
    source: Literal["config", "keychain", "env", "file"] | None = None


def _read_key_file() -> str | None:
    base = os.environ.get("CAO_PLUGIN_DATA") or str(Path.home() / ".config" / "cao")
    path = Path(base) / "gemini_api_key"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_keychain() -> str | None:
    """Return the Gemini API key from the OS keychain, or None if unavailable.

    Never raises: a missing or broken keyring backend (common on headless servers)
    must fall through to the next auth tier, not crash the daemon.

    ponytail: a present-but-hung D-Bus can stall the first keyring call ~2min before the
    except fires (degrades to a slow start, not a crash). Env-only headless users bypass
    it instantly with PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring.
    """
    try:
        import keyring  # ponytail: deferred — import probes backends and can stall

        return keyring.get_password("cao", "gemini_api_key") or None
    except Exception:  # noqa: BLE001 — any backend failure means "no keychain key"
        return None


def _resolve_api_key(
    explicit: str | None,
) -> tuple[str, Literal["config", "keychain", "env", "file"]] | None:
    """Resolve a Gemini API key by BL-26 precedence, returning (key, source) or None.

    Order: explicit config value, then OS keychain, then GEMINI_API_KEY env
    (Google's recommended location), then plaintext key file (last resort).
    """
    if explicit:
        return explicit, "config"
    keychain = _read_keychain()
    if keychain:
        return keychain, "keychain"
    env = (os.environ.get("GEMINI_API_KEY") or "").strip() or None
    if env:
        return env, "env"
    file_key = _read_key_file()
    if file_key:
        return file_key, "file"
    return None


def _adc_file_path() -> Path:
    override = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    return Path(override) if override else Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _detect_adc_project() -> str | None:
    """Return the GCP project for Vertex, or None. Never raises.

    Order: google.auth.default() (env / gcloud / service account) → the ADC file's
    quota_project_id (set by `gcloud auth application-default login`; google.auth.default()
    does NOT return it for authorized_user creds) → `gcloud config get-value project`.
    """
    try:
        import google.auth  # ponytail: deferred — ships with google-genai; probes gcloud/metadata

        _creds, project = google.auth.default()
        if project:
            return str(project)
    except Exception:  # noqa: BLE001 — any ADC failure falls through to the next source
        pass
    try:
        raw = json.loads(_adc_file_path().read_text(encoding="utf-8"))
        qp = raw.get("quota_project_id")
        if qp:
            return str(qp)
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=5,
        )
        val = out.stdout.strip()
        if val and val != "(unset)":
            return val
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def resolve_auth(config: dict[str, Any] | None = None) -> AuthConfig:
    """Resolve credentials: explicit config dict first, then env vars.

    Precedence:
    config api_key → OS keychain → GEMINI_API_KEY → key file → AuthNotConfigured.
    """
    if config is None:
        config = defaults.load() or None
    model = os.environ.get("CAO_MODEL", _DEFAULT_MODEL)

    if config is not None:
        model = str(config.get("model", model))
        mode = str(config.get("mode", ""))
        if mode == "gemini_api_key":
            # api_key is NEVER persisted in defaults.json (only the mode is), so
            # resolve by precedence; raise (not KeyError) if no tier yields a key.
            resolved = _resolve_api_key(config.get("api_key"))
            if resolved is None:
                raise AuthNotConfigured()
            key, source = resolved
            return AuthConfig(
                mode="gemini_api_key",
                model=model,
                project=None,
                location=None,
                api_key=key,
                source=source,
            )
        if mode == "vertex":
            project = (
                str(config["project"]) if config.get("project")
                else os.environ.get("GOOGLE_CLOUD_PROJECT") or _detect_adc_project()
            )
            if not project:
                raise AuthNotConfigured()
            return AuthConfig(
                mode="vertex",
                model=model,
                project=project,
                location=str(config["location"]) if config.get("location") else os.environ.get("GOOGLE_CLOUD_LOCATION", _DEFAULT_LOCATION),
                api_key=None,
            )

    # Fall back to env (BL-26 precedence: keychain → env → key file)
    resolved = _resolve_api_key(None)
    if resolved is not None:
        key, source = resolved
        return AuthConfig(
            mode="gemini_api_key",
            model=model,
            project=None,
            location=None,
            api_key=key,
            source=source,
        )

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or _detect_adc_project()
    if project:
        return AuthConfig(
            mode="vertex",
            model=model,
            project=project,
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", _DEFAULT_LOCATION),
            api_key=None,
        )

    raise AuthNotConfigured()


def to_local_agent_kwargs(
    auth: AuthConfig, *, model: str | None = None, effort: str | None = None
) -> dict[str, Any]:
    """Return LocalAgentConfig auth kwargs + model key.

    Precedence for the effective model is CLI ``model`` > ``auth.model`` (already
    ``CAO_MODEL`` > default). When ``effort`` is set the
    model becomes a ``ModelTarget`` whose endpoint carries the
    ``GeminiModelOptions.thinking_level`` (there is no ``ModelTarget.thinking_level``).
    """
    effective_id = model or auth.model
    base: dict[str, Any] = {}
    if auth.mode == "gemini_api_key":
        base["api_key"] = auth.api_key
    else:  # vertex
        base["vertex"] = True
        base["project"] = auth.project
        base["location"] = auth.location

    if effort is None:
        base["model"] = effective_id
        return base

    opts = GeminiModelOptions(thinking_level=ThinkingLevel(effort))
    endpoint = (
        VertexEndpoint(project=auth.project, location=auth.location, options=opts)
        if auth.mode == "vertex"
        else GeminiAPIEndpoint(api_key=auth.api_key, options=opts)
    )
    # ponytail: shorthand auth kept alongside the explicit-endpoint ModelTarget is
    # harmless — it only feeds the default image model (SDK keeps our endpoint verbatim).
    base["model"] = ModelTarget(name=effective_id, endpoint=endpoint)
    return base
