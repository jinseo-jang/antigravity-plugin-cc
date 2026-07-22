---
description: Set persistent model/region defaults (written to defaults.json; takes effect on the next session)
argument-hint: "[--model <id>] [--location <region>] [--mode vertex|gemini_api_key] [--project <id>]"
allowed-tools:
  - Bash(python:*)
  - AskUserQuestion
---

Persist model/region defaults to `$CAO_PLUGIN_DATA/defaults.json` (or `~/.config/cao/defaults.json`).
These are read fresh on every session start — no daemon restart needed.

**Supported models:**

| Model | Status | Notes |
|---|---|---|
| `gemini-3.6-flash` | GA | Default. |
| `gemini-3.5-flash` | GA | |
| `gemini-3.5-flash-lite` | GA | |
| `gemini-3.1-pro-preview` | Public Preview | Narrower regional availability. |

Any other model string is rejected with `-32602` before the worker starts. **Region is your choice** — `global` (the default) works for all models; pick any Vertex location where your model is available (Gemini's regional coverage keeps expanding).

**Workflow:**

1. Use **AskUserQuestion** to ask the user which mode they want: `vertex` (Vertex AI via ADC) or `gemini_api_key` (Gemini API key).
2. For `vertex` mode, ask for `--model` (`gemini-3.6-flash` (default), `gemini-3.5-flash`, `gemini-3.5-flash-lite`, or `gemini-3.1-pro-preview`) AND `--location` — recommend `global` (today the only region these Gemini-3 models are reliably served on); let the user type another Vertex region only if they know their model is available there (an unavailable region hangs until the worker-turn timeout, it does not fail fast). If they choose nothing, `global` is used. Also ask for the **GCP project id** — tell the user they can leave it blank to let agy auto-detect it from ADC (`quota_project_id`) or the active gcloud config. For `gemini_api_key` mode, ask for `--model` only (key stays in `GEMINI_API_KEY`; no location or project).
3. Run the companion, including `--project <id>` only if the user gave one (omit it entirely to let auto-detect handle it):

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" setup --mode <mode> [--model <id>] [--location <region>] [--project <id>]

4. If the companion prints a rejection (unsupported model), relay the **Options** from the message via **AskUserQuestion** and retry with a corrected model.
5. On success, confirm to the user that the defaults are saved and will apply to the next `/agy:implement` invocation.

**Notes:**
- `--api-key` is never accepted or stored. Provide the key one of three ways, checked in this order: (1) **OS keychain** — `python -m keyring set cao gemini_api_key` (encrypted at rest, recommended); (2) **`GEMINI_API_KEY` env var** — export it in the shell that launches Claude Code, then restart it (Google's recommended location); (3) **plaintext file** `~/.config/cao/gemini_api_key` (chmod 600) — last resort, **not encrypted**.
- If the companion warns that no key is found (gemini_api_key mode), point the user at the keychain command first, then the env-var or file fallbacks.
- **Region is user-selectable.** `global` is the default (works for both models) and is used when you omit `--location`; pick another Vertex region where your model is served. An unavailable model×region is not blocked at setup — it surfaces as a worker timeout, so choose a region where the model actually runs.
- `gemini-3.1-pro-preview` is the correct model code for Gemini 3.1 Pro (bare `gemini-3.1-pro` is a 404).
- Defaults take effect on the **next** session (fresh-read, no daemon restart required).
- To clear defaults, delete `defaults.json` directly.
