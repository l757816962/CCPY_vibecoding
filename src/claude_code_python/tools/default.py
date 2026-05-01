from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..messages import ToolResult
from .base import BaseTool, ToolContext, ToolSpec
from .registry import ToolRegistry

IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ccpy",
}
READ_MAX_OUTPUT_LINES = 4000
GLOB_MAX_RESULTS = 500
GREP_MAX_FILE_BYTES = 2_000_000


def text_result(tool: str, content: str, data: Any = None, is_error: bool = False) -> ToolResult:
    return ToolResult(tool_call_id="", name=tool, content=content, data=data, is_error=is_error)


def _iter_workspace_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIR_NAMES]
        base = Path(dirpath)
        for name in filenames:
            yield base / name


class ReadInput(BaseModel):
    path: str
    offset: int | None = None
    limit: int | None = None


class ReadTool(BaseTool):
    spec = ToolSpec("Read", "Read a UTF-8 text file from the workspace.", ReadInput, True, True)

    async def call(self, payload: ReadInput, context: ToolContext) -> ToolResult:
        path = context.permissions.require_workspace_path(payload.path, "read")
        start = max((payload.offset or 1) - 1, 0)
        requested_limit = payload.limit
        effective_limit = READ_MAX_OUTPUT_LINES if requested_limit is None else min(requested_limit, READ_MAX_OUTPUT_LINES)
        body_lines: list[str] = []
        truncated = False

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if line_no - 1 < start:
                    continue
                if len(body_lines) >= effective_limit:
                    truncated = True
                    break
                body_lines.append(f"{line_no}|{line.rstrip('\r\n')}")

        body = "\n".join(body_lines)
        if truncated or (requested_limit is not None and requested_limit > effective_limit):
            body = (
                f"{body}\n\n[Truncated] Maximum returned lines: {effective_limit}. "
                "Use offset/limit to read additional content."
            ).strip()
        return text_result(self.name, body or "File is empty.")


class WriteInput(BaseModel):
    path: str
    content: str


class WriteTool(BaseTool):
    spec = ToolSpec("Write", "Write a UTF-8 file inside the workspace.", WriteInput)

    async def call(self, payload: WriteInput, context: ToolContext) -> ToolResult:
        path = context.permissions.require_workspace_path(payload.path, "write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.content, encoding="utf-8")
        return text_result(self.name, f"Wrote {path}")


class EditInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(BaseTool):
    spec = ToolSpec("Edit", "Replace text in an existing workspace file.", EditInput)

    async def call(self, payload: EditInput, context: ToolContext) -> ToolResult:
        path = context.permissions.require_workspace_path(payload.path, "edit")
        text = path.read_text(encoding="utf-8")
        count = text.count(payload.old_string)
        if count == 0:
            raise ValueError("old_string was not found")
        if count > 1 and not payload.replace_all:
            raise ValueError("old_string matched multiple locations; set replace_all=true")
        updated = text.replace(payload.old_string, payload.new_string, -1 if payload.replace_all else 1)
        path.write_text(updated, encoding="utf-8")
        return text_result(self.name, f"Edited {path}; replacements={count if payload.replace_all else 1}")


class GlobInput(BaseModel):
    pattern: str
    path: str = "."


class GlobTool(BaseTool):
    spec = ToolSpec("Glob", "Find workspace files matching a glob pattern.", GlobInput, True, True)

    async def call(self, payload: GlobInput, context: ToolContext) -> ToolResult:
        root = context.permissions.require_workspace_path(payload.path, "glob")
        pattern = payload.pattern.replace("\\", "/")
        match_name_only = "/" not in pattern and "**" not in pattern

        matches: list[str] = []
        for file in _iter_workspace_files(root):
            rel = str(file.relative_to(context.workspace)).replace("\\", "/")
            candidate = file.name if match_name_only else rel
            if fnmatch.fnmatch(candidate, pattern):
                matches.append(rel)
        matches.sort()

        truncated = len(matches) > GLOB_MAX_RESULTS
        shown = matches[:GLOB_MAX_RESULTS]
        body = "\n".join(shown) or "No files found."
        if truncated:
            body += f"\n\n[Truncated] Showing first {GLOB_MAX_RESULTS} of {len(matches)} matches."
        return text_result(self.name, body, shown)


class GrepInput(BaseModel):
    pattern: str
    path: str = "."
    include: str | None = None
    case_insensitive: bool = False
    head_limit: int = 100


class GrepTool(BaseTool):
    spec = ToolSpec("Grep", "Search file contents with a Python regular expression.", GrepInput, True, True)

    async def call(self, payload: GrepInput, context: ToolContext) -> ToolResult:
        root = context.permissions.require_workspace_path(payload.path, "grep")
        flags = re.IGNORECASE if payload.case_insensitive else 0
        regex = re.compile(payload.pattern, flags)
        output: list[str] = []
        skipped_large = 0
        for file in _iter_workspace_files(root):
            rel = str(file.relative_to(context.workspace))
            if payload.include and not fnmatch.fnmatch(file.name, payload.include):
                continue
            try:
                if file.stat().st_size > GREP_MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
            except OSError:
                continue
            try:
                with file.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        text = line.rstrip("\n")
                        if regex.search(text):
                            output.append(f"{rel}:{line_no}:{text}")
                            if len(output) >= payload.head_limit:
                                body = "\n".join(output)
                                if skipped_large:
                                    body += f"\n\n[Info] Skipped large files: {skipped_large}"
                                return text_result(self.name, body, output)
            except OSError:
                continue
        body = "\n".join(output) or "No matches found."
        if skipped_large:
            body += f"\n\n[Info] Skipped large files: {skipped_large}"
        return text_result(self.name, body, output)


class BashInput(BaseModel):
    command: str
    working_directory: str = "."
    timeout_ms: int = 30_000
    run_in_background: bool = False


class BashTool(BaseTool):
    spec = ToolSpec("Bash", "Run a shell command in the workspace.", BashInput)

    async def call(self, payload: BashInput, context: ToolContext) -> ToolResult:
        context.permissions.can_run_command(payload.command)
        cwd = context.permissions.require_workspace_path(payload.working_directory, "bash")
        shell = context.config.default_shell

        async def run_command() -> str:
            proc = await asyncio.create_subprocess_exec(
                *shell,
                payload.command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=payload.timeout_ms / 1000)
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                return f"Timed out after {payload.timeout_ms}ms\n{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
            return (
                f"exit_code={proc.returncode}\n"
                f"stdout:\n{stdout.decode(errors='replace')[:20000]}\n"
                f"stderr:\n{stderr.decode(errors='replace')[:20000]}"
            )

        if payload.run_in_background:
            managed = context.task_manager.create(payload.command, kind="shell", owner_id=context.runner.owner_id)

            async def worker() -> None:
                try:
                    context.task_manager.complete(
                        managed.id,
                        await run_command(),
                        owner_id=context.runner.owner_id,
                    )
                except Exception as exc:
                    context.task_manager.fail(managed.id, str(exc), owner_id=context.runner.owner_id)

            managed.handle = asyncio.create_task(worker())
            return text_result(self.name, f"Launched shell task {managed.id}. Output: {managed.output_file}")
        return text_result(self.name, await run_command())


class TodoItem(BaseModel):
    id: str
    content: str
    status: str = Field(pattern="^(pending|in_progress|completed|cancelled)$")


class TodoWriteInput(BaseModel):
    todos: list[TodoItem]
    merge: bool = True


class TodoWriteTool(BaseTool):
    spec = ToolSpec("TodoWrite", "Create or update a structured todo list.", TodoWriteInput)

    async def call(self, payload: TodoWriteInput, context: ToolContext) -> ToolResult:
        incoming = [todo.model_dump() for todo in payload.todos]
        if payload.merge:
            by_id = {todo["id"]: todo for todo in context.todos}
            by_id.update({todo["id"]: todo for todo in incoming})
            context.todos[:] = list(by_id.values())
        else:
            context.todos[:] = incoming
        return text_result(self.name, f"Todos updated:\n{context.todos}", context.todos)


class WebFetchInput(BaseModel):
    url: str


class WebFetchTool(BaseTool):
    spec = ToolSpec("WebFetch", "Fetch text content from an HTTP URL.", WebFetchInput, True, True)

    async def call(self, payload: WebFetchInput, context: ToolContext) -> ToolResult:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(payload.url)
        response.raise_for_status()
        return text_result(self.name, response.text[:30000])


class WebSearchInput(BaseModel):
    query: str
    max_results: int = 5


class WebSearchTool(BaseTool):
    spec = ToolSpec("WebSearch", "Search the web using Tavily if configured, otherwise DuckDuckGo instant answer.", WebSearchInput, True, True)

    async def call(self, payload: WebSearchInput, context: ToolContext) -> ToolResult:
        tavily_key = os.getenv("TAVILY_API_KEY")
        async with httpx.AsyncClient(timeout=30) as client:
            if tavily_key:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": payload.query, "max_results": payload.max_results},
                )
                response.raise_for_status()
                return text_result(self.name, str(response.json())[:20000], response.json())
            response = await client.get("https://api.duckduckgo.com/", params={"q": payload.query, "format": "json"})
            response.raise_for_status()
            data = response.json()
        return text_result(self.name, str(data)[:20000], data)


class AskUserQuestionInput(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class AskUserQuestionTool(BaseTool):
    spec = ToolSpec("AskUserQuestion", "Ask the user a clarification question.", AskUserQuestionInput)

    async def call(self, payload: AskUserQuestionInput, context: ToolContext) -> ToolResult:
        opts = "\n".join(f"- {item}" for item in payload.options)
        return text_result(self.name, f"Question for user: {payload.question}\n{opts}".strip())


class ExitPlanModeInput(BaseModel):
    plan: str


class ExitPlanModeTool(BaseTool):
    spec = ToolSpec("ExitPlanMode", "Return a plan and exit planning mode.", ExitPlanModeInput)

    async def call(self, payload: ExitPlanModeInput, context: ToolContext) -> ToolResult:
        return text_result(self.name, payload.plan)


class TaskInput(BaseModel):
    description: str
    prompt: str
    subagent_type: str = "general"
    run_in_background: bool = False
    model: str | None = None
    allowed_tools: list[str] | None = None


class AgentTool(BaseTool):
    spec = ToolSpec("Agent", "Launch a sub-agent. Alias: Task.", TaskInput, aliases=("Task",))

    async def call(self, payload: TaskInput, context: ToolContext) -> ToolResult:
        denied = {"Agent", "AskUserQuestion", "ExitPlanMode"}
        allowed = set(payload.allowed_tools) if payload.allowed_tools else None
        registry = context.registry.filter(denied=denied, allowed=allowed)
        sub_runner = context.runner.clone_for_subagent(registry)

        async def run_subagent() -> str:
            extra = (
                context.task_manager.drain_messages(task.id, owner_id=context.runner.owner_id)
                if "task" in locals()
                else []
            )
            prompt = payload.prompt
            if extra:
                prompt += "\n\nQueued messages:\n" + "\n".join(extra)
            result = await sub_runner.run(
                prompt,
                registry=registry,
                model=payload.model,
                max_turns=context.config.max_turns,
                persist=True,
                system_prompt=f"You are a {payload.subagent_type} sub-agent. Complete the delegated task and return a concise result.",
            )
            return result.text

        if payload.run_in_background:
            task = context.task_manager.create(
                payload.description,
                kind="agent",
                owner_id=context.runner.owner_id,
            )

            async def worker() -> None:
                try:
                    context.task_manager.complete(
                        task.id,
                        await run_subagent(),
                        owner_id=context.runner.owner_id,
                    )
                except asyncio.CancelledError:
                    context.task_manager.complete(
                        task.id,
                        "Task was cancelled.",
                        status="killed",
                        owner_id=context.runner.owner_id,
                    )
                except Exception as exc:
                    context.task_manager.fail(task.id, str(exc), owner_id=context.runner.owner_id)

            task.handle = asyncio.create_task(worker())
            return text_result(self.name, f"Launched agent {task.id}. Output: {task.output_file}")

        result = await run_subagent()
        return text_result(self.name, result)


class TaskOutputInput(BaseModel):
    task_id: str


class TaskOutputTool(BaseTool):
    spec = ToolSpec("TaskOutput", "Read the status and output of a background task.", TaskOutputInput, True, True)

    async def call(self, payload: TaskOutputInput, context: ToolContext) -> ToolResult:
        task = context.task_manager.require(payload.task_id, owner_id=context.runner.owner_id)
        output = task.output_file.read_text(encoding="utf-8") if task.output_file.exists() else task.result
        return text_result(self.name, f"status={task.status}\noutput_file={task.output_file}\n{output}")


class TaskStopInput(BaseModel):
    task_id: str


class TaskStopTool(BaseTool):
    spec = ToolSpec("TaskStop", "Stop a running background task.", TaskStopInput)

    async def call(self, payload: TaskStopInput, context: ToolContext) -> ToolResult:
        return text_result(
            self.name,
            await context.task_manager.stop(payload.task_id, owner_id=context.runner.owner_id),
        )


class SendMessageInput(BaseModel):
    task_id: str
    message: str


class SendMessageTool(BaseTool):
    spec = ToolSpec("SendMessage", "Queue a message for a running background agent.", SendMessageInput)

    async def call(self, payload: SendMessageInput, context: ToolContext) -> ToolResult:
        context.task_manager.queue_message(
            payload.task_id,
            payload.message,
            owner_id=context.runner.owner_id,
        )
        return text_result(self.name, f"Queued message for {payload.task_id}")


def build_default_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            BashTool(),
            TodoWriteTool(),
            WebFetchTool(),
            WebSearchTool(),
            AskUserQuestionTool(),
            ExitPlanModeTool(),
            AgentTool(),
            TaskOutputTool(),
            TaskStopTool(),
            SendMessageTool(),
        ]
    )
