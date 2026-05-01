from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compact import compact_messages, reactive_compact_messages
from .config import Config
from .hooks import HookContext, HookEvent, HookManager, HookResult
from .messages import AssistantTurn, ToolCall, ToolResult, new_id
from .model import ModelError
from .memory import append_project_memory
from .permissions import PermissionManager
from .providers import LLMProvider, create_provider
from .session import JSONLSessionStore, SessionNotesStore
from .tasks import TaskManager
from .tools.registry import ToolRegistry

SYSTEM_PROMPT = """You are Claude-Code-Python, a terminal coding agent.
Use tools when they help. When multiple independent investigations are useful,
call the Agent/Task tool multiple times in the same turn so they run in parallel.
Always keep tool inputs valid JSON and summarize tool results clearly."""


@dataclass(slots=True)
class RunResult:
    text: str
    messages: list[dict[str, Any]]
    turns: int
    tool_counts: dict[str, int] = field(default_factory=dict)
    subagent_tool_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RunContext:
    config: Config
    workspace: Path
    permissions: PermissionManager
    registry: ToolRegistry
    model_client: LLMProvider
    task_manager: TaskManager
    runner: "AgentRunner"
    todos: list[dict[str, Any]] = field(default_factory=list)
    hook_manager: HookManager | None = None


@dataclass(slots=True)
class ToolStats:
    counts: dict[str, int] = field(default_factory=dict)
    subagent_counts: dict[str, int] = field(default_factory=dict)

    def record(self, name: str, depth: int) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1
        if depth > 0:
            self.subagent_counts[name] = self.subagent_counts.get(name, 0) + 1


class AgentRunner:
    def __init__(
        self,
        config: Config | None = None,
        registry: ToolRegistry | None = None,
        model_client: LLMProvider | None = None,
        session_store: JSONLSessionStore | None = None,
        task_manager: TaskManager | None = None,
        tool_stats: ToolStats | None = None,
        hook_manager: HookManager | None = None,
        depth: int = 0,
        owner_id: str | None = None,
    ):
        self.config = config or Config.from_env()
        self.permissions = PermissionManager(self.config.workspace, self.config.permission_mode)
        self.model_client = model_client or create_provider(self.config)
        self.task_manager = task_manager or TaskManager(self.config.task_output_dir)
        self.registry = registry or ToolRegistry()
        self.session_store = session_store or JSONLSessionStore(self.config.session_dir)
        self.session_notes = SessionNotesStore(self.session_store) if self.config.session_notes_enabled else None
        self.todos: list[dict[str, Any]] = []
        self.tool_stats = tool_stats or ToolStats()
        self.hook_manager = hook_manager or HookManager()
        self.depth = depth
        self.owner_id = owner_id or new_id("owner")

    def context(self, registry: ToolRegistry | None = None) -> RunContext:
        return RunContext(
            config=self.config,
            workspace=self.config.workspace,
            permissions=self.permissions,
            registry=registry or self.registry,
            model_client=self.model_client,
            task_manager=self.task_manager,
            runner=self,
            todos=self.todos,
            hook_manager=self.hook_manager,
        )

    async def run(
        self,
        prompt: str,
        *,
        registry: ToolRegistry | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        persist: bool = True,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> RunResult:
        messages = self.initial_messages(system_prompt=system_prompt)
        return await self.continue_run(
            messages,
            prompt,
            registry=registry,
            model=model,
            max_turns=max_turns,
            persist=persist,
        )

    def initial_messages(self, *, system_prompt: str = SYSTEM_PROMPT) -> list[dict[str, Any]]:
        effective_system_prompt = self.effective_system_prompt(system_prompt)
        return [{"role": "system", "content": effective_system_prompt}]

    def effective_system_prompt(self, system_prompt: str = SYSTEM_PROMPT) -> str:
        return (
            append_project_memory(
                system_prompt,
                self.config.workspace,
                max_chars=self.config.project_memory_max_chars,
            )
            if self.config.project_memory_enabled
            else system_prompt
        )

    async def continue_run(
        self,
        messages: list[dict[str, Any]],
        prompt: str,
        *,
        registry: ToolRegistry | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        persist: bool = True,
    ) -> RunResult:
        active_registry = registry or self.registry
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, self.initial_messages()[0])
        messages.append({"role": "user", "content": prompt})
        if persist:
            self.session_store.record("user", prompt)

        final_text = ""
        turns = 0
        while turns < (max_turns or self.config.max_turns):
            turns += 1
            messages = await self._compact(messages)
            try:
                assistant = await self.model_client.complete(
                    messages=messages,
                    tools=active_registry.schemas(),
                    model=model,
                )
            except ModelError as exc:
                if not self.config.compact_reactive_enabled or not _is_prompt_too_long(exc):
                    raise
                messages = await self._compact(messages, reactive=True)
                assistant = await self.model_client.complete(
                    messages=messages,
                    tools=active_registry.schemas(),
                    model=model,
                )
            messages.append(assistant.to_openai_message())
            await self._execute_hooks(HookEvent.POST_MODEL_CALL, messages, metadata={"turn": turns})
            if persist:
                self.session_store.record("assistant", assistant.to_openai_message())
            self._update_session_notes(messages)
            final_text = assistant.content or final_text

            if not assistant.tool_calls:
                await self._execute_hooks(HookEvent.STOP, messages, metadata={"turn": turns})
                return RunResult(
                    text=assistant.content,
                    messages=messages,
                    turns=turns,
                    tool_counts=dict(self.tool_stats.counts),
                    subagent_tool_counts=dict(self.tool_stats.subagent_counts),
                )

            tool_results = await self._run_tools(assistant.tool_calls, active_registry)
            for result in tool_results:
                messages.append(result.to_openai_message())
                if persist:
                    self.session_store.record("tool", result.to_openai_message())
            self._update_session_notes(messages)

        return RunResult(
            text=final_text or f"Stopped after reaching max_turns={max_turns or self.config.max_turns}",
            messages=messages,
            turns=turns,
            tool_counts=dict(self.tool_stats.counts),
            subagent_tool_counts=dict(self.tool_stats.subagent_counts),
        )

    async def _compact(self, messages: list[dict[str, Any]], reactive: bool = False) -> list[dict[str, Any]]:
        await self._execute_hooks(HookEvent.PRE_COMPACT, messages, metadata={"reactive": reactive})
        session_summary = self.session_notes.summary() if self.session_notes else ""
        if reactive:
            compacted = reactive_compact_messages(
                messages,
                max_tokens=self.config.compact_max_tokens,
                session_summary=session_summary,
            )
        else:
            compacted = compact_messages(
                messages,
                max_tokens=self.config.compact_max_tokens,
                recent_messages=self.config.compact_recent_messages,
                tool_result_max_chars=self.config.compact_tool_result_max_chars,
                session_summary=session_summary,
            )
        await self._execute_hooks(
            HookEvent.POST_COMPACT,
            compacted,
            metadata={"reactive": reactive, "before": len(messages), "after": len(compacted)},
        )
        return compacted

    async def _run_tools(self, calls: list[ToolCall], registry: ToolRegistry) -> list[ToolResult]:
        results: list[ToolResult] = []
        batch: list[ToolCall] = []

        async def flush_batch() -> None:
            nonlocal batch
            if not batch:
                return
            semaphore = asyncio.Semaphore(self.config.max_tool_concurrency)

            async def run_one(call: ToolCall) -> ToolResult:
                async with semaphore:
                    return await self._run_one_tool(call, registry)

            results.extend(await asyncio.gather(*(run_one(call) for call in batch)))
            batch = []

        for call in calls:
            tool = registry.get(call.name)
            if tool and tool.spec.is_concurrency_safe:
                batch.append(call)
                continue
            await flush_batch()
            results.append(await self._run_one_tool(call, registry))
        await flush_batch()
        return results

    async def _run_one_tool(self, call: ToolCall, registry: ToolRegistry) -> ToolResult:
        self.tool_stats.record(call.name, self.depth)
        tool = registry.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        hook_results = await self._execute_hooks(
            HookEvent.PRE_TOOL_USE,
            [],
            tool_name=call.name,
            tool_input=call.input,
        )
        for hook_result in hook_results:
            if hook_result.updated_input is not None:
                call.input = hook_result.updated_input
            if hook_result.blocking_error:
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=hook_result.blocking_error,
                    is_error=True,
                )
        result = await tool.execute(call.id, call.input, self.context(registry))
        result.tool_call_id = call.id
        result.name = call.name
        await self._execute_hooks(
            HookEvent.POST_TOOL_USE,
            [],
            tool_name=call.name,
            tool_input=call.input,
            tool_output=result.content,
            tool_error=result.content if result.is_error else None,
        )
        return result

    def clone_for_subagent(self, registry: ToolRegistry) -> "AgentRunner":
        return AgentRunner(
            config=self.config,
            registry=registry,
            model_client=self.model_client,
            session_store=self.session_store,
            task_manager=self.task_manager,
            tool_stats=self.tool_stats,
            hook_manager=self.hook_manager,
            depth=self.depth + 1,
            owner_id=self.owner_id,
        )

    async def _execute_hooks(
        self,
        event: HookEvent,
        messages: list[dict[str, Any]],
        *,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_output: str | None = None,
        tool_error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[HookResult]:
        if not self.config.hooks_enabled:
            return []
        return await self.hook_manager.execute(
            event,
            HookContext(
                messages=messages,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                tool_error=tool_error,
                runner=self,
                metadata=metadata or {},
            ),
        )

    def _update_session_notes(self, messages: list[dict[str, Any]]) -> None:
        if self.session_notes is not None:
            self.session_notes.update(messages)


def _is_prompt_too_long(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "prompt_too_long",
            "context_length_exceeded",
            "maximum context length",
            "request too large",
            "too many tokens",
            "413",
        )
    )
