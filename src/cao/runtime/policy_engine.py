"""PolicyEngine: pure configurator — assembles SDK Policy objects from workspace config.

Never evaluates policies. All evaluation is delegated to sdk_policy.enforce().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from google.antigravity import types  # type: ignore[import-untyped]
from google.antigravity.hooks import policy as sdk_policy  # type: ignore[import-untyped]
from google.antigravity.hooks.policy import AskUserHandler  # type: ignore[import-untyped]

logger = logging.getLogger("cao.policy")

# ---------------------------------------------------------------------------
# Credential-path predicate — operates on ToolCall.canonical_path; traversal-safe.
# Annotated as types.ToolCall so the SDK's _evaluate_predicate passes the full
# ToolCall object (not tool_call.args). Returns False for non-file tools whose
# canonical_path is None — fail-safe, never over-denies.
# ---------------------------------------------------------------------------

_SECRET_NAMES: frozenset[str] = frozenset({".env"})
_SECRET_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".crt", ".p12"})
_SECRET_STEMS: frozenset[str] = frozenset({"id_rsa", "id_ed25519", "id_dsa"})


def _is_credential_path(tc: types.ToolCall) -> bool:
    """Return True (deny applies) when canonical_path points to a secret file.

    Covers .env/.env.local/.env.production, *.pem, *.key, *.crt, *.p12,
    id_rsa, id_ed25519, id_dsa.  Grep/search tools that carry canonical_path
    are covered by the deny("*") wildcard.
    """
    if not tc.canonical_path:
        return False
    p = Path(tc.canonical_path)
    return (
        p.name in _SECRET_NAMES
        or p.name.startswith(".env")
        or p.suffix in _SECRET_SUFFIXES
        or p.stem in _SECRET_STEMS
    )


# ---------------------------------------------------------------------------
# Workspace config schema (TOML → Pydantic)
# ---------------------------------------------------------------------------


# Security: state-mutating tools blocked in read-only review sessions — stripped
# at the harness (capabilities.disabled_tools) AND denied by policy (defense-in-depth).
_MUTATING_TOOLS: tuple[types.BuiltinTools, ...] = (
    types.BuiltinTools.CREATE_FILE,
    types.BuiltinTools.EDIT_FILE,
    types.BuiltinTools.RUN_COMMAND,
    types.BuiltinTools.GENERATE_IMAGE,
    types.BuiltinTools.START_SUBAGENT,
)


class WorkspaceConfig(BaseModel):
    workspace_root: str
    review: bool = False


# ---------------------------------------------------------------------------
# PolicyEngine — pure configurator
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Pure configurator: assembles SDK Policy objects from WorkspaceConfig.

    Never evaluates. Callers pass the returned list to sdk_policy.enforce().
    """

    def __init__(self, approval_waiter: Any = None) -> None:
        # ponytail: approval_waiter ignored; kept for daemon backward-compat
        self._last_config: WorkspaceConfig | None = None  # stored for hook backward-compat

    def build_policies(
        self,
        config: WorkspaceConfig,
        approval_handler: AskUserHandler | None = None,
    ) -> list[Any]:
        """Build and return a flat list of SDK Policy objects.

        Evaluation priority (SDK bucket system):
          Level 0  SPECIFIC_DENY  — workspace_only file-tool denies
          Level 6  GLOBAL_DENY    — deny("*", when=credential) fires after
                                    workspace checks but before allow("*")
          Level 8  GLOBAL_ALLOW   — allow("*") from confirm_run_command

        ponytail: no explicit safe-read allows added. allow("*") from
        confirm_run_command covers them at level 8, after the credential
        deny at level 6. Adding allow("view_file") at level 2 (SPECIFIC_ALLOW)
        would short-circuit before the credential deny — a security hole.
        """
        self._last_config = config  # stored for hook backward-compat
        policies: list[Any] = [
            # 1. Secret deny — wildcard + predicate; GLOBAL_DENY level 6
            sdk_policy.deny("*", when=_is_credential_path, name="deny_secrets"),
            # 2. Workspace containment — SPECIFIC_DENY level 0 for file tools
            *sdk_policy.workspace_only([config.workspace_root]),
        ]
        if config.review:
            # Read-only: SPECIFIC_DENY each mutating tool (level 0, beats allow("*")),
            # then allow("*") so read tools pass while the credential deny still holds.
            policies += [
                sdk_policy.deny(t.value, name=f"review_deny_{t.value}") for t in _MUTATING_TOOLS
            ]
            policies.append(sdk_policy.allow("*", name="review_allow_readonly"))
        else:
            # run_command handling + allow("*") fallback (GLOBAL_ALLOW level 8)
            policies += list(sdk_policy.confirm_run_command(approval_handler))
        logger.debug(
            "built %d policies for workspace=%s review=%s",
            len(policies),
            config.workspace_root,
            config.review,
        )
        return policies
