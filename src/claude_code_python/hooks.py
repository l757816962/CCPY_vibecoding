from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_MODEL_CALL = "post_model_call"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    STOP = "stop"


@dataclass(slots=True)
class HookContext:
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    tool_error: str | None = None
    runner: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookResult:
    blocking_error: str | None = None
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    prevent_continuation: bool = False


HookCallback = Callable[[HookContext], Awaitable[HookResult | None]]


@dataclass(slots=True)
class _RegisteredHook:
    name: str
    callback: HookCallback
    priority: int = 0
    fire_and_forget: bool = False


class HookManager:
    def __init__(self):
        self._hooks: dict[HookEvent, list[_RegisteredHook]] = {event: [] for event in HookEvent}
        self._background_tasks: set[asyncio.Task] = set()

    def register(
        self,
        event: HookEvent,
        callback: HookCallback,
        *,
        name: str = "",
        priority: int = 0,
        fire_and_forget: bool = False,
    ) -> None:
        entry = _RegisteredHook(
            name=name or getattr(callback, "__name__", "anonymous"),
            callback=callback,
            priority=priority,
            fire_and_forget=fire_and_forget,
        )
        self._hooks[event].append(entry)
        self._hooks[event].sort(key=lambda item: item.priority)

    async def execute(self, event: HookEvent, context: HookContext) -> list[HookResult]:
        results: list[HookResult] = []
        for entry in self._hooks.get(event, []):
            if entry.fire_and_forget:
                task = asyncio.create_task(entry.callback(context))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                continue
            result = await entry.callback(context)
            if result is not None:
                results.append(result)
                if result.blocking_error or result.prevent_continuation:
                    break
        return results

    def list_hooks(self) -> dict[str, list[str]]:
        return {
            event.value: [entry.name for entry in entries]
            for event, entries in self._hooks.items()
            if entries
        }

    async def shutdown(self) -> None:
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
