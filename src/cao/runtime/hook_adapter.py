"""CAO hook adapters — thin subclasses of SDK hook base classes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from google.antigravity import types  # type: ignore[import-untyped]
from google.antigravity.hooks import (  # type: ignore[import-untyped]
    OnSessionEndHook as _SessionEndBase,
    OnSessionStartHook as _SessionStartBase,
    OnToolErrorHook as _ErrBase,
    PostToolCallHook as _PostBase,
    PreToolCallDecideHook as _PreBase,
)
from google.antigravity.hooks import policy as sdk_policy
from google.antigravity.hooks.hooks import HookResult  # type: ignore[import-untyped]
from google.antigravity.hooks.policy import AskUserHandler  # type: ignore[import-untyped]

from cao.models import SECRET_PATH_MASK, ApprovalDecision
from cao.runtime import approval_store
from cao.runtime.approval_waiter import ApprovalWaiter

if TYPE_CHECKING:
    from cao.runtime.digest_generator import DigestGenerator
    from cao.runtime.git_diff_collector import GitDiffCollector
    from cao.runtime.policy_engine import WorkspaceConfig
    from cao.runtime.session_manager import SessionManager

logger = logging.getLogger("cao.hooks")

_DEFAULT_TIMEOUT: float = 300.0

# BL-25: PolicyEngine names the secret-file deny "deny_secrets"; the SDK echoes
# that name in the deny message ("Denied by policy 'deny_secrets'."). Branch on
# it to mask secret paths while a workspace-containment deny keeps its real path.
_SECRET_DENY_POLICY: str = "deny_secrets"

AsyncPublish = Callable[[str, str, dict[str, Any]], Awaitable[None]]


async def _noop_publish(session_id: str, event_type: str, payload: dict[str, Any]) -> None:
    pass


# ---------------------------------------------------------------------------
# make_approval_handler — AskUserHandler backed by ApprovalWaiter + EventBus
# ---------------------------------------------------------------------------


def make_approval_handler(
    waiter: ApprovalWaiter,
    publish: AsyncPublish,
    session_id_getter: Callable[[], str],
    timeout_seconds: float = _DEFAULT_TIMEOUT,
    session_manager: Any = None,
    on_approval_triggered: Callable[[], None] | None = None,
) -> AskUserHandler:
    """Factory: AskUserHandler that suspends via ApprovalWaiter and publishes events.

    Publishes approval.required, suspends session to "suspended", then publishes
    tool.approved or tool.denied. Timeout → denied (reason=timeout).
    """

    async def handler(tool_call: Any) -> bool:
        call_id = waiter.next_call_id()
        sid = session_id_getter()
        tool_name = str(getattr(tool_call, "name", ""))
        args = getattr(tool_call, "args", {})
        command = (
            str(args["command_line"])
            if isinstance(args, dict) and args.get("command_line")
            else f"{tool_name} {args}"[:200]
        )
        workspace = ""
        if session_manager is not None:
            sess = session_manager.get_session(sid)
            if sess is not None:
                workspace = sess.workspace
        if approval_store.is_allowed(command, workspace):
            await publish(
                sid,
                "approval.auto_allowed",
                {"call_id": call_id, "command": command, "scope": "remembered"},
            )
            return True
        if on_approval_triggered is not None:
            on_approval_triggered()
        if session_manager is not None:
            session_manager.transition(sid, "suspended")
        # Register the pending future BEFORE publishing approval.required: a
        # consumer reacting to the event must never resolve() before the call_id
        # is in the registry, else session.approve/deny races into -32602.
        fut = waiter.register_pending(call_id, command=command, session_id=sid)
        await publish(
            sid,
            "approval.required",
            {
                "call_id": call_id,
                "tool": tool_name,
                "command": command,
                "arguments": str(args)[:500],
            },
        )
        try:
            decision: ApprovalDecision = await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            waiter.remove_if_pending(call_id)
            await publish(sid, "tool.denied", {"call_id": call_id, "reason": "timeout"})
            return False
        except asyncio.CancelledError:
            waiter.remove_if_pending(call_id)
            return False
        finally:
            if session_manager is not None:
                session_manager.transition(sid, "running")
        if decision == ApprovalDecision.ALLOW:
            await publish(sid, "tool.approved", {"call_id": call_id})
            return True
        await publish(sid, "tool.denied", {"call_id": call_id})
        return False

    return handler


# ---------------------------------------------------------------------------
# CAOPreToolCallDecideHook
# ---------------------------------------------------------------------------


class CAOPreToolCallDecideHook(_PreBase):  # type: ignore[misc]
    """Thin adapter: build SDK enforcer once, publish tool.requested, delegate.

    Tracks whether the approval handler fired (_approval_triggered) so that:
    - auto-allowed tools emit tool.auto_allowed
    - policy-denied tools emit tool.denied
    - human-approved/denied tools emit nothing extra (handler already published)
    """

    def __init__(
        self,
        policies: list[Any] | None = None,
        event_bus: AsyncPublish | None = None,
        # Backward-compat kwargs (e2e conftest and build_agent_config use these):
        policy_engine: Any = None,
        approval_waiter: ApprovalWaiter | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        session_manager: Any = None,
    ) -> None:
        bus = event_bus or _noop_publish
        self._bus: AsyncPublish = bus
        self._session_cell: list[str] | None = None
        self._approval_triggered: bool = False

        if policies is None:
            config: WorkspaceConfig | None = getattr(policy_engine, "_last_config", None)
            if config is not None and approval_waiter is not None:
                session_cell: list[str] = ["unknown"]
                self._session_cell = session_cell

                def _mark_triggered() -> None:
                    self._approval_triggered = True

                handler = make_approval_handler(
                    approval_waiter,
                    bus,
                    lambda: session_cell[0],
                    timeout_seconds,
                    session_manager=session_manager,
                    on_approval_triggered=_mark_triggered,
                )
                policies = policy_engine.build_policies(config, handler)
            else:
                policies = []

        self._policies = policies  # exposed for build_agent_config → LocalAgentConfig
        self._enforcer = sdk_policy.enforce(policies)

    async def run(self, context: Any, data: Any) -> Any:
        self._approval_triggered = False
        try:
            session_id: str = context.get_state("session_id") or "unknown"
            if self._session_cell is not None:
                self._session_cell[0] = session_id
            call_id = str(uuid.uuid4())
            context.set_state("call_id", call_id)
            # Normalize legacy FakeToolCall (.tool_name) → types.ToolCall (.name)
            if not hasattr(data, "name"):
                cp = getattr(data, "canonical_path", None)
                data = types.ToolCall(
                    name=getattr(data, "tool_name", ""),
                    canonical_path=str(cp) if cp else None,
                    args=getattr(data, "arguments", {}),
                )
            tool_name: str = str(data.name)
            await self._bus(
                session_id, "tool.requested", {"call_id": call_id, "tool_name": tool_name}
            )
            result = await self._enforcer.run(context, data)
            canonical_path = getattr(data, "canonical_path", None)
            path_str = str(canonical_path) if canonical_path is not None else None
            if result.allow:
                if not self._approval_triggered:
                    await self._bus(
                        session_id,
                        "tool.auto_allowed",
                        {"call_id": call_id, "tool_name": tool_name, "path": path_str},
                    )
            else:
                if not self._approval_triggered:
                    # Tag the deny by policy so the digest counts ONLY true
                    # workspace-containment breaches (BL-25): the SDK echoes the
                    # policy name in result.message, as trusted for deny_secrets.
                    msg = result.message or ""
                    reason: str
                    path: str | None
                    if _SECRET_DENY_POLICY in msg:
                        reason, path = "deny_secrets", SECRET_PATH_MASK
                    elif "review_deny" in msg:
                        reason, path = "review_readonly", path_str
                    else:
                        reason, path = "workspace_containment", path_str
                    await self._bus(
                        session_id,
                        "tool.denied",
                        {"call_id": call_id, "reason": reason, "path": path},
                    )
            return result
        except Exception:
            logger.exception("unexpected error in PreToolCallDecideHook — defaulting to Deny")
            return HookResult(allow=False, message="unexpected error")


# ---------------------------------------------------------------------------
# CAOPostToolCallHook
# ---------------------------------------------------------------------------


class CAOPostToolCallHook(_PostBase):  # type: ignore[misc]
    """Thin adapter: publish tool.completed on successful tool call."""

    def __init__(self, event_bus: AsyncPublish | None = None) -> None:
        self._bus: AsyncPublish = event_bus or _noop_publish

    async def run(self, context: Any, data: Any) -> None:
        try:
            session_id: str = context.get_state("session_id") or "unknown"
            call_id: str = context.get_state("call_id") or "unknown"
            await self._bus(session_id, "tool.completed", {"call_id": call_id})
        except Exception:
            logger.exception("error in PostToolCallHook")


# ---------------------------------------------------------------------------
# CAOOnToolErrorHook
# ---------------------------------------------------------------------------


class CAOOnToolErrorHook(_ErrBase):  # type: ignore[misc]
    """Thin adapter: log tool errors; return the exception for SDK retry logic."""

    def __init__(self, event_bus: AsyncPublish | None = None) -> None:
        self._bus: AsyncPublish = event_bus or _noop_publish

    async def run(self, context: Any, data: Any) -> Any:
        try:
            session_id: str = context.get_state("session_id") or "unknown"
            call_id: str = context.get_state("call_id") or "unknown"
            err_type = type(data).__name__ if data is not None else "unknown"
            await self._bus(
                session_id, "tool.failed", {"call_id": call_id, "error": err_type}
            )
        except Exception:
            logger.exception("error in OnToolErrorHook")
        return data


# ---------------------------------------------------------------------------
# CAOOnSessionStartHook
# ---------------------------------------------------------------------------


class CAOOnSessionStartHook(_SessionStartBase):  # type: ignore[misc]
    """Thin adapter: set session_id and workspace_path on SessionContext."""

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        event_bus: AsyncPublish | None = None,
        git_diff_collector: GitDiffCollector | None = None,
        task_description: str = "",
    ) -> None:
        self._session_id = session_id
        self._workspace_path = workspace_path
        self._bus: AsyncPublish = event_bus or _noop_publish
        self._git_diff = git_diff_collector
        self._task_description = task_description

    async def run(self, context: Any, data: Any) -> None:
        try:
            context.set_state("session_id", self._session_id)
            context.set_state("workspace_path", self._workspace_path)
            if self._git_diff is not None:
                base_commit = await self._git_diff.before_snapshot(self._workspace_path)
                context.set_state("base_commit", base_commit)
            await self._bus(
                self._session_id,
                "session.started",
                {"workspace": self._workspace_path, "task": self._task_description},
            )
        except Exception:
            logger.exception("error in OnSessionStartHook")


# ---------------------------------------------------------------------------
# CAOOnSessionEndHook
# ---------------------------------------------------------------------------


class CAOOnSessionEndHook(_SessionEndBase):  # type: ignore[misc]
    """Thin adapter: publish session.ended FIRST (so DigestGenerator sees it on disk),
    then render the digest."""

    def __init__(
        self,
        event_bus: AsyncPublish | None = None,
        git_diff_collector: GitDiffCollector | None = None,
        digest_generator: DigestGenerator | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._bus: AsyncPublish = event_bus or _noop_publish
        self._git_diff = git_diff_collector
        self._digest_gen = digest_generator
        self._session_manager = session_manager

    async def run(self, context: Any, data: Any) -> None:
        try:
            session_id: str = context.get_state("session_id") or "unknown"
            workspace_path: str = context.get_state("workspace_path") or ""
            base_commit: str | None = context.get_state("base_commit")

            # Publish session.ended first so it's in events.jsonl before
            # DigestGenerator.render() reads disk as its primary source.
            await self._bus(session_id, "session.ended", {"status": "completed"})

            if self._session_manager is not None:
                self._session_manager.transition(session_id, "done")

            if self._git_diff is not None and workspace_path:
                diff_summary = await self._git_diff.after_snapshot(workspace_path, base_commit)
                if self._digest_gen is not None:
                    digest = self._digest_gen.render(session_id, diff_summary)
                    await self._bus(
                        session_id, "digest.ready", {"digest_path": digest.digest_path}
                    )
        except Exception:
            logger.exception("error in OnSessionEndHook")
