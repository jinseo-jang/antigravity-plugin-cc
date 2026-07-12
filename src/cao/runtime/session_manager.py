"""SessionManager: one active session per workspace slug."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from google.antigravity import (  # type: ignore[import-untyped]
    Agent,
    LocalAgentConfig,
)
from google.antigravity.types import CapabilitiesConfig  # type: ignore[import-untyped]

from cao.models import Session
from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.auth import resolve_auth, to_local_agent_kwargs
from cao.runtime.digest_generator import DigestGenerator
from cao.runtime.event_bus import EventBus, make_publish_wrapper
from cao.runtime.git_diff_collector import GitDiffCollector
from cao.runtime.hook_adapter import (
    CAOOnSessionEndHook,
    CAOOnSessionStartHook,
    CAOOnToolErrorHook,
    CAOPostToolCallHook,
    CAOPreToolCallDecideHook,
)
from cao.runtime.policy_engine import _MUTATING_TOOLS, PolicyEngine, WorkspaceConfig
from cao.runtime import session_store, transcript

logger = logging.getLogger("cao.session")

_AsyncPublish = Callable[[str, str, dict[str, Any]], Awaitable[None]]

# ponytail: 600s bounds an otherwise-infinite harness hang on an unreachable
# model/location; raise CAO_TURN_TIMEOUT for legitimately long single turns.
_TURN_TIMEOUT: float = float(os.environ.get("CAO_TURN_TIMEOUT", "600"))


async def _noop_publish(session_id: str, event_type: str, payload: dict[str, Any]) -> None:
    pass


class SessionAlreadyActiveError(Exception):
    """Raised when create_session is called for a slug with an active session."""


class SessionManager:
    """Manages session lifecycle; enforces one-active-session-per-slug (Broker rule)."""

    def __init__(
        self,
        approval_waiter: ApprovalWaiter,
        event_bus: EventBus | None = None,
        git_diff_collector: GitDiffCollector | None = None,
        digest_generator: DigestGenerator | None = None,
        auth_config: dict[str, Any] | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._waiter = approval_waiter
        self._sessions: dict[str, Session] = {}
        self._active: dict[str, str] = {}  # slug -> session_id
        self._event_bus = event_bus
        self._git_diff = git_diff_collector
        self._digest_gen = digest_generator
        self._auth_config = auth_config
        # ponytail: Agent is the real SDK factory; tests inject a fake async-cm callable
        self._agent_factory: Callable[..., Any] = (
            agent_factory if agent_factory is not None else Agent
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_desc: dict[str, str] = {}
        self._task_opts: dict[str, dict[str, Any]] = {}

    def __deepcopy__(self, memo: dict[int, Any]) -> "SessionManager":
        # Live service, not a value: SDK deepcopies the agent config whose hooks
        # hold this manager; they must call back into the same live instance.
        return self

    def start_task(
        self,
        session_id: str,
        task: str,
        review: bool = False,
        *,
        model: str | None = None,
        effort: str | None = None,
        files: list[str] | None = None,
        conversation_id: str | None = None,
        save_dir: str | None = None,
        system_instructions: str | None = None,
    ) -> None:
        """Spawn a background task that runs the agent turn (non-blocking)."""
        self._task_desc[session_id] = task
        opts: dict[str, Any] = {}
        if model is not None:
            opts["model"] = model
        if effort is not None:
            opts["effort"] = effort
        if files is not None:
            opts["files"] = files
        if conversation_id is not None:
            opts["conversation_id"] = conversation_id
        if save_dir is not None:
            opts["save_dir"] = save_dir
        self._task_opts[session_id] = opts
        self._tasks[session_id] = asyncio.create_task(
            self._run_and_track(
                session_id,
                task,
                review,
                model=model,
                effort=effort,
                files=files,
                conversation_id=conversation_id,
                save_dir=save_dir,
                system_instructions=system_instructions,
            )
        )

    async def _run_and_track(
        self,
        session_id: str,
        task: str,
        review: bool = False,
        *,
        model: str | None = None,
        effort: str | None = None,
        files: list[str] | None = None,
        conversation_id: str | None = None,
        save_dir: str | None = None,
        system_instructions: str | None = None,
    ) -> None:
        try:
            await asyncio.wait_for(
                self.run_task(
                    session_id,
                    task,
                    review,
                    model=model,
                    effort=effort,
                    files=files,
                    conversation_id=conversation_id,
                    save_dir=save_dir,
                    system_instructions=system_instructions,
                ),
                timeout=_TURN_TIMEOUT,
            )
            self.transition(session_id, "done")
        except asyncio.TimeoutError:
            self.transition(session_id, "timed_out")
            publish: _AsyncPublish = (
                make_publish_wrapper(self._event_bus)
                if self._event_bus is not None
                else _noop_publish
            )
            await publish(
                session_id,
                "session.ended",
                {
                    "status": "timed_out",
                    "reason": (
                        f"worker turn exceeded {_TURN_TIMEOUT:g}s — the model may be"
                        " unavailable for the resolved location, or --effort was used"
                        " with a model that lacks thinking_level support (needs a"
                        " Gemini-3 model on GOOGLE_CLOUD_LOCATION=global)"
                    ),
                },
            )
            logger.warning("session %s turn timed out after %ss", session_id, _TURN_TIMEOUT)
        except asyncio.CancelledError:
            self.transition(session_id, "cancelled")
            raise
        except Exception as exc:
            logger.exception("session %s task crashed", session_id)
            self.transition(session_id, "crashed")
            crash_publish: _AsyncPublish = (
                make_publish_wrapper(self._event_bus)
                if self._event_bus is not None
                else _noop_publish
            )
            await crash_publish(
                session_id,
                "session.ended",
                {"status": "crashed", "reason": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            self._tasks.pop(session_id, None)
            for slug, sid in list(self._active.items()):
                if sid == session_id:
                    del self._active[slug]
                    break

    async def get_events(self, session_id: str, after_event_id: int = 0) -> list[Any]:
        """Return persisted events for the session (empty if no EventBus)."""
        if self._event_bus is None:
            return []
        return await self._event_bus.get_events(session_id, after_event_id)

    def get_last_task(self, session_id: str) -> str | None:
        return self._task_desc.get(session_id)

    def get_task_opts(self, session_id: str) -> dict[str, Any]:
        """The opts (model/effort/files/conversation_id/save_dir) the last task for
        this session ran with, as a copy so callers can mutate it freely."""
        return dict(self._task_opts.get(session_id, {}))

    async def create_session(
        self, slug: str, workspace: str, task: str = ""
    ) -> Session:
        """Create a new running session for *slug*.

        Raises SessionAlreadyActiveError if a running/suspended session already
        exists for this slug (Broker rule from concurrency_model.md §8.1).
        """
        if slug in self._active:
            existing_id = self._active[slug]
            existing = self._sessions.get(existing_id)
            if existing is not None and existing.state in ("running", "suspended"):
                raise SessionAlreadyActiveError(slug)
            # stale entry — clean up
            del self._active[slug]

        session = Session(
            session_id=str(uuid.uuid4()),
            workspace=workspace,
            state="running",
            created_at=datetime.now(timezone.utc),
        )
        self._sessions[session.session_id] = session
        self._active[slug] = session.session_id
        logger.info("session %s created for slug=%s task=%r", session.session_id, slug, task)
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_active_session_id(self, slug: str) -> str | None:
        return self._active.get(slug)

    def active_session_id(self) -> str | None:
        """The current active session id (any slug), for commands that target it
        without an explicit id (e.g. cancel with {} — Task 004 §RPC)."""
        return next(iter(self._active.values()), None)

    def latest_session_id(self) -> str | None:
        """The most-recently-created session id, or None. Used to resolve the
        retry target when no id is supplied; persists after the session leaves
        _active on completion (so a finished session is still retryable)."""
        if not self._sessions:
            return None
        return max(self._sessions.values(), key=lambda s: s.created_at).session_id

    def list_sessions(self) -> list[dict[str, str]]:
        """All known sessions as {session_id, state} dicts, newest first."""
        ordered = sorted(
            self._sessions.values(), key=lambda s: s.created_at, reverse=True
        )
        return [{"session_id": s.session_id, "state": s.state} for s in ordered]

    async def cancel_session(self, session_id: str) -> None:
        """Transition session to cancelled; cancel all pending approvals."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        bg = self._tasks.pop(session_id, None)
        if bg is not None and not bg.done():
            bg.cancel()
        self._waiter.cancel_all()
        self._sessions[session_id] = session.model_copy(update={"state": "cancelled"})
        for slug, sid in list(self._active.items()):
            if sid == session_id:
                del self._active[slug]
                break
        logger.info("session %s cancelled", session_id)

    def transition(self, session_id: str, state: str) -> None:
        """Update session state (idle/running/suspended/done/cancelled/crashed/timed_out)."""
        session = self._sessions.get(session_id)
        if session is not None:
            self._sessions[session_id] = session.model_copy(update={"state": state})

    def build_agent_config(
        self,
        session_id: str,
        workspace: str,
        task: str = "",
        review: bool = False,
        *,
        model: str | None = None,
        effort: str | None = None,
        files: list[str] | None = None,
        conversation_id: str | None = None,
        save_dir: str | None = None,
        system_instructions: str | None = None,
    ) -> Any:
        publish: _AsyncPublish = (
            make_publish_wrapper(self._event_bus)
            if self._event_bus is not None
            else _noop_publish
        )
        pe = PolicyEngine(approval_waiter=self._waiter)
        pe._last_config = WorkspaceConfig(workspace_root=workspace, review=review)
        pre_hook = CAOPreToolCallDecideHook(
            policies=None,
            event_bus=publish,
            policy_engine=pe,
            approval_waiter=self._waiter,
            session_manager=self,
        )
        hooks: list[Any] = [
            pre_hook,
            CAOPostToolCallHook(event_bus=publish),
            CAOOnToolErrorHook(event_bus=publish),
            CAOOnSessionStartHook(
                session_id=session_id,
                workspace_path=workspace,
                event_bus=publish,
                git_diff_collector=self._git_diff,
                task_description=task,
            ),
            CAOOnSessionEndHook(
                event_bus=publish,
                git_diff_collector=self._git_diff,
                digest_generator=self._digest_gen,
                session_manager=self,
            ),
        ]
        auth = resolve_auth(self._auth_config)
        extra: dict[str, Any] = {}
        if review:
            # Strip mutating tools at the harness so the model cannot call them,
            # independent of Python decide-hook timing (SDK-recommended for read-only).
            extra["capabilities"] = CapabilitiesConfig(disabled_tools=list(_MUTATING_TOOLS))

        # --- Wave-1 seams (no-op in Wave 0). Each `if` is one task's edit region. ---
        if files:
            pass  # ponytail: Task 008 fills — from_file() attachments (prompt assembly in run_task)
        if save_dir is not None:
            # ponytail: stable save_dir always; without it the SDK mkdtemps and
            # the trajectory is unrecoverable (ADR-0005).
            extra["save_dir"] = save_dir
        if conversation_id is not None:
            extra["conversation_id"] = conversation_id
        if system_instructions:
            # Task 010: handoff transcript summary → system_instructions.
            # Compose onto any existing base; never clobber. Today build_agent_config sets
            # no base, so base=None and the preamble is the base worker instruction.
            extra["system_instructions"] = transcript.build_handoff_instructions(
                system_instructions, base=extra.get("system_instructions"),
            )

        # policies=[] is load-bearing: it suppresses LocalAgentConfig's
        # default_factory=confirm_run_command. The SDK still auto-registers a 2nd
        # enforce hook (from the always-prepended workspace_only), but with no
        # confirm_run_command, so run_command is asked once — by our hook, which
        # runs first in the deny-wins runner and stays authoritative.
        return LocalAgentConfig(
            workspaces=[workspace],
            policies=[],
            hooks=hooks,
            **extra,
            **to_local_agent_kwargs(auth, model=model, effort=effort),
        )

    async def run_task(
        self,
        session_id: str,
        task: str,
        review: bool = False,
        *,
        model: str | None = None,
        effort: str | None = None,
        files: list[str] | None = None,
        conversation_id: str | None = None,
        save_dir: str | None = None,
        system_instructions: str | None = None,
    ) -> str:
        # ponytail: files/conversation_id/system_instructions are threaded as the
        # single typed seam Tasks 008/009/010 flip on; no producer in Wave 0.
        session = self._sessions.get(session_id)
        workspace = session.workspace if session is not None else ""
        publish: _AsyncPublish = (
            make_publish_wrapper(self._event_bus)
            if self._event_bus is not None
            else _noop_publish
        )
        effective_save_dir = save_dir or session_store.trajectory_dir(workspace, session_id)
        effective_id = model or resolve_auth(self._auth_config).model
        await publish(session_id, "session.model", {"model": effective_id, "effort": effort or "default"})
        cfg = self.build_agent_config(
            session_id,
            workspace,
            task,
            review,
            model=model,
            effort=effort,
            files=files,
            conversation_id=conversation_id,
            save_dir=effective_save_dir,
            system_instructions=system_instructions,
        )
        # files holds parts already resolved at the daemon boundary (R5), not paths.
        prompt: Any = [task, *files] if files else task
        async with self._agent_factory(config=cfg) as agent:
            resp = await agent.chat(prompt)
            text = str(await resp.text())
            # BL-4: publish inside the agent context, before it exits — the digest is
            # rendered by the session-end hook on that exit, so it must see this event.
            await publish(session_id, "session.response", {"text": text})
            conv_raw: Any = agent.conversation_id
            conv_id = conv_raw if isinstance(conv_raw, str) else None
            session_store.record(workspace, session_id, conv_id, effective_save_dir)
            return text
