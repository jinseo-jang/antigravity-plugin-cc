from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.antigravity.types import ToolCall  # type: ignore[import-untyped]


@dataclass
class FakeToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    canonical_path: str | None = None
    filesystem_mutation: Callable[[Path], None] | None = None

    def to_sdk_call(self) -> ToolCall:
        return ToolCall(
            name=self.tool_name,
            args=self.arguments,
            id=self.call_id,
            canonical_path=self.canonical_path,
        )


class FakeHookContext:
    def __init__(self) -> None:
        self._state: dict[str, Any] = {}

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


class FakeAgent:
    def __init__(self) -> None:
        self._hooks: dict[str, Any] = {}

    def register(self, slot: str, hook: Any) -> None:
        self._hooks[slot] = hook

    async def run_scenario(
        self,
        workspace: Path,
        calls: list[FakeToolCall],
        context: FakeHookContext | None = None,
    ) -> None:
        ctx: FakeHookContext = context if context is not None else FakeHookContext()

        start: Any = self._hooks.get("session_start")
        if start is not None:
            await start.run(ctx, None)

        for call in calls:
            sdk_call = call.to_sdk_call()
            pre: Any = self._hooks.get("pre_tool")
            raw_result: Any = (await pre.run(ctx, sdk_call)) if pre is not None else None
            allowed: bool = getattr(raw_result, "allow", raw_result == "allow") if raw_result is not None else True

            if allowed:
                if call.filesystem_mutation is not None:
                    call.filesystem_mutation(workspace)
                post: Any = self._hooks.get("post_tool")
                if post is not None:
                    await post.run(ctx, sdk_call)

        end: Any = self._hooks.get("session_end")
        if end is not None:
            await end.run(ctx, None)


class FakeConversation:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent
