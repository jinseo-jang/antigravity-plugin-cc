"""Tests for PolicyEngine — SDK-native configurator.

All behavior assertions go through real sdk_policy.enforce().run() — not our code.
"""

from __future__ import annotations

import pytest

from google.antigravity import types  # type: ignore[import-untyped]
from google.antigravity.hooks import policy as sdk_policy  # type: ignore[import-untyped]
from google.antigravity.hooks.hooks import SessionContext  # type: ignore[import-untyped]

from cao.runtime.policy_engine import PolicyEngine, WorkspaceConfig


def _cfg(workspace: str = "/workspace") -> WorkspaceConfig:
    return WorkspaceConfig(workspace_root=workspace)


def _cfg_review(workspace: str = "/workspace") -> WorkspaceConfig:
    return WorkspaceConfig(workspace_root=workspace, review=True)


def _ctx() -> SessionContext:
    return SessionContext()


def _tc(name: str, canonical_path: str | None = None) -> types.ToolCall:
    return types.ToolCall(name=name, canonical_path=canonical_path)


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


# ---------------------------------------------------------------------------
# Structure: build_policies returns real SDK Policy objects
# ---------------------------------------------------------------------------


def test_build_policies_returns_sdk_policy_list(engine: PolicyEngine) -> None:
    policies = engine.build_policies(_cfg())
    assert isinstance(policies, list)
    assert len(policies) > 0
    assert all(isinstance(p, sdk_policy.Policy) for p in policies)


def test_deny_secrets_policy_present(engine: PolicyEngine) -> None:
    policies = engine.build_policies(_cfg())
    assert any(p.name == "deny_secrets" for p in policies)


# ---------------------------------------------------------------------------
# AC: .env access → deny — proven via real sdk_policy.enforce().run()
# ---------------------------------------------------------------------------


async def test_env_deny_real_enforce(engine: PolicyEngine) -> None:
    """PROOF: .env → deny via real SDK evaluation (not our code)."""
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/.env"))
    assert result.allow is False


async def test_env_local_deny_real_enforce(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/.env.local"))
    assert result.allow is False


async def test_env_production_deny_real_enforce(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/.env.production"))
    assert result.allow is False


async def test_pem_deny_real_enforce(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/server.pem"))
    assert result.allow is False


async def test_key_deny_real_enforce(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/server.key"))
    assert result.allow is False


async def test_id_rsa_deny_real_enforce(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/id_rsa"))
    assert result.allow is False


# ---------------------------------------------------------------------------
# workspace_only: outside workspace → deny
# ---------------------------------------------------------------------------


async def test_outside_workspace_deny(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/etc/passwd"))
    assert result.allow is False


async def test_outside_workspace_shadow_deny(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("edit_file", "/etc/shadow"))
    assert result.allow is False


# ---------------------------------------------------------------------------
# Safe file inside workspace → allow
# ---------------------------------------------------------------------------


async def test_safe_file_inside_workspace_allow(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("view_file", "/workspace/src/main.py"))
    assert result.allow is True


# ---------------------------------------------------------------------------
# run_command: deny without handler, ask with handler
# ---------------------------------------------------------------------------


async def test_run_command_no_handler_deny(engine: PolicyEngine) -> None:
    gate = sdk_policy.enforce(engine.build_policies(_cfg()))
    result = await gate.run(_ctx(), _tc("run_command"))
    assert result.allow is False


async def test_run_command_with_handler_invokes_ask(engine: PolicyEngine) -> None:
    called: list[types.ToolCall] = []

    async def handler(tc: types.ToolCall) -> bool:
        called.append(tc)
        return True

    gate = sdk_policy.enforce(engine.build_policies(_cfg(), approval_handler=handler))
    result = await gate.run(_ctx(), _tc("run_command"))
    assert result.allow is True
    assert len(called) == 1


async def test_run_command_handler_returning_false_denies(engine: PolicyEngine) -> None:
    async def handler(tc: types.ToolCall) -> bool:
        return False

    gate = sdk_policy.enforce(engine.build_policies(_cfg(), approval_handler=handler))
    result = await gate.run(_ctx(), _tc("run_command"))
    assert result.allow is False


# ---------------------------------------------------------------------------
# Review mode: read-only enforced via real sdk_policy.enforce().run()
# ---------------------------------------------------------------------------


async def test_review_edit_file_inside_workspace_denied() -> None:
    """PROOF of enforcement: edit_file is allowed normally but denied in review."""
    normal = sdk_policy.enforce(PolicyEngine().build_policies(_cfg()))
    assert (await normal.run(_ctx(), _tc("edit_file", "/workspace/src/main.py"))).allow is True

    review = sdk_policy.enforce(PolicyEngine().build_policies(_cfg_review()))
    assert (await review.run(_ctx(), _tc("edit_file", "/workspace/src/main.py"))).allow is False


async def test_review_denies_all_mutating_tools() -> None:
    review = sdk_policy.enforce(PolicyEngine().build_policies(_cfg_review()))
    for tool in ("create_file", "edit_file", "run_command", "generate_image", "start_subagent"):
        result = await review.run(_ctx(), _tc(tool, "/workspace/src/main.py"))
        assert result.allow is False, f"{tool} should be denied in review mode"


async def test_review_allows_read_only_tools() -> None:
    review = sdk_policy.enforce(PolicyEngine().build_policies(_cfg_review()))
    assert (await review.run(_ctx(), _tc("view_file", "/workspace/src/main.py"))).allow is True


async def test_review_still_denies_secrets_and_escape() -> None:
    review = sdk_policy.enforce(PolicyEngine().build_policies(_cfg_review()))
    assert (await review.run(_ctx(), _tc("view_file", "/workspace/.env"))).allow is False
    assert (await review.run(_ctx(), _tc("view_file", "/etc/passwd"))).allow is False


# ---------------------------------------------------------------------------
# Malformed config raises
# ---------------------------------------------------------------------------


def test_malformed_config_raises() -> None:
    with pytest.raises(Exception):
        WorkspaceConfig.model_validate({})
