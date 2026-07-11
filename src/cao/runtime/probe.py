"""Region capability probe — fast-fail for a model x location the worker can't serve (BL-20).

`check_region_available` runs BEFORE a worker turn: for Vertex it does one bounded
`generate_content` (the only call that is region-sensitive — `models.get` is NOT),
so a non-servable region yields an immediate -32602 recovery message instead of a
~600s turn-timeout hang. Successes are cached; gemini_api_key mode has no location
and is skipped; transient errors (timeout/5xx) allow the turn through.

# ponytail: verdict from the live call's HTTP status/message; no retries (the worker
# turn retries). Bound via asyncio.wait_for.
"""

from __future__ import annotations

import asyncio
import importlib
import os

from cao.runtime import probe_cache
from cao.runtime.auth import AuthConfig

_PROBE_TIMEOUT: float = float(os.environ.get("CAO_PROBE_TIMEOUT", "10"))


async def check_region_available(auth: AuthConfig, model: str) -> str | None:
    """None if the model is servable at the resolved location (or not checkable), else a
    -32602-worthy recovery message. Vertex-only; a cached success skips the live call."""
    if auth.mode != "vertex":
        return None
    if probe_cache.is_ok(auth.project, model, auth.location):
        return None
    verdict = await _probe(auth, model)
    if verdict == "ok":
        probe_cache.mark_ok(auth.project, model, auth.location)
        return None
    if verdict == "unavailable":
        return (
            f"Model '{model}' is not available at location '{auth.location}'."
            " Options: re-run with --location global, or set a servable region via /agy:setup."
        )
    return None  # transient -> let the worker turn proceed


def _verdict_for_error(exc: BaseException) -> str:
    """Map a probe error to 'unavailable' vs 'transient'. Only 404/NOT_FOUND is treated
    as the definitive 'region does not serve this model' — 400 is a catch-all (a thinking
    model rejecting max_output_tokens=1, a safety filter, a malformed request), so it
    allows through rather than falsely blocking a servable region."""
    code = getattr(exc, "code", None)
    text = str(exc)
    if code == 404 or "NOT_FOUND" in text or "was not found" in text:
        return "unavailable"
    return "transient"


async def _probe(auth: AuthConfig, model: str) -> str:
    """One bounded live generate_content. Returns 'ok' | 'unavailable' | 'transient'."""
    # importlib (not a direct import) so mypy does not follow google.genai -> numpy,
    # whose stubs use 3.12 `type` syntax that breaks mypy under python_version 3.11.
    try:
        genai = importlib.import_module("google.genai")
        gtypes = importlib.import_module("google.genai.types")
    except ImportError:
        return "transient"  # SDK absent -> don't block; the turn surfaces it

    def _call() -> None:
        client = genai.Client(
            vertexai=True,
            project=auth.project,
            location=auth.location,
            http_options=gtypes.HttpOptions(timeout=int(_PROBE_TIMEOUT * 1000)),
        )
        client.models.generate_content(
            model=model,
            contents="ping",
            config=gtypes.GenerateContentConfig(max_output_tokens=1),
        )

    try:
        await asyncio.wait_for(asyncio.to_thread(_call), timeout=_PROBE_TIMEOUT + 2.0)
    except asyncio.TimeoutError:
        return "transient"
    except Exception as exc:  # noqa: BLE001 - map any SDK/HTTP error to a verdict
        return _verdict_for_error(exc)
    return "ok"
