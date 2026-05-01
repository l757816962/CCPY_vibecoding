from __future__ import annotations

import json
from pathlib import Path

import pytest
import httpx
from typer.testing import CliRunner

from claude_code_python.config import Config
from claude_code_python.compact import compact_messages, estimate_tokens_messages, micro_compact_tool_results
from claude_code_python.hooks import HookEvent, HookManager, HookResult
from claude_code_python.cli import app, tool_call_names
from claude_code_python.mcp import MCPBridgeTool, MCPManager
from claude_code_python.memory import load_project_memory, scan_memory_files
from claude_code_python.messages import AssistantTurn, ToolCall, normalize_tool_call_ids
from claude_code_python.model import ModelError, OpenAICompatibleClient
from claude_code_python.providers import PROVIDER_PRESETS, apply_provider_preset, create_provider
from claude_code_python.runner import AgentRunner
from claude_code_python.session import SessionNotesStore
from claude_code_python.tasks import TaskManager
from claude_code_python.tools.default import build_default_registry


class FakeModel:
    def __init__(self, turns: list[AssistantTurn], prompt_routes: dict[str, AssistantTurn] | None = None):
        self.turns = turns
        self.prompt_routes = prompt_routes or {}
        self.seen_messages: list[list[dict]] = []

    async def complete(self, messages, tools, model=None):
        self.seen_messages.append(messages)
        for message in messages:
            content = str(message.get("content", ""))
            for marker, turn in self.prompt_routes.items():
                if marker in content:
                    return turn
        return self.turns.pop(0)


class FlakyHttpClient:
    def __init__(self):
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


def config(tmp_path: Path) -> Config:
    return Config.from_env(workspace=tmp_path)


@pytest.mark.asyncio
async def test_tool_result_pairing_for_read(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    fake = FakeModel(
        [
            AssistantTurn(tool_calls=[ToolCall(id="call_1", name="Read", input={"path": "hello.txt"})]),
            AssistantTurn(content="Read complete."),
        ]
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    result = await runner.run("read hello")

    assert result.text == "Read complete."
    assert any(message.get("role") == "tool" and message.get("tool_call_id") == "call_1" for message in result.messages)


@pytest.mark.asyncio
async def test_bash_tool_executes(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            AssistantTurn(tool_calls=[ToolCall(id="call_1", name="Bash", input={"command": "echo ccpy"})]),
            AssistantTurn(content="done"),
        ]
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    result = await runner.run("run echo")

    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert "ccpy" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_bash_tool_blocks_dangerous_command(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="Bash",
                        input={"command": "Remove-Item -Recurse -Force C:\\temp"},
                    )
                ]
            ),
            AssistantTurn(content="done"),
        ]
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    result = await runner.run("dangerous")

    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert "dangerous command denied by policy" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_parallel_safe_tools_are_executed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    fake = FakeModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="call_a", name="Read", input={"path": "a.txt"}),
                    ToolCall(id="call_b", name="Read", input={"path": "b.txt"}),
                ]
            ),
            AssistantTurn(content="both read"),
        ]
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    result = await runner.run("read both")

    tool_ids = {message.get("tool_call_id") for message in result.messages if message.get("role") == "tool"}
    assert {"call_a", "call_b"} <= tool_ids


@pytest.mark.asyncio
async def test_read_tool_truncates_large_output(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line-{idx}" for idx in range(1, 4105)), encoding="utf-8")
    registry = build_default_registry()
    runner = AgentRunner(config=config(tmp_path), registry=registry, model_client=FakeModel([]))

    result = await runner._run_one_tool(ToolCall(id="call_read", name="Read", input={"path": "big.txt"}), registry)

    assert "[Truncated] Maximum returned lines: 4000" in result.content
    assert "4000|line-4000" in result.content
    assert "4001|line-4001" not in result.content


@pytest.mark.asyncio
async def test_glob_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hidden.py").write_text("x=1", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "visible.py").write_text("x=2", encoding="utf-8")
    registry = build_default_registry()
    runner = AgentRunner(config=config(tmp_path), registry=registry, model_client=FakeModel([]))

    result = await runner._run_one_tool(ToolCall(id="call_glob", name="Glob", input={"pattern": "*.py"}), registry)

    assert "src/visible.py" in result.content
    assert ".venv/hidden.py" not in result.content


@pytest.mark.asyncio
async def test_grep_skips_ignored_dirs_and_large_files(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "secret.txt").write_text("TOKEN", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "public.txt").write_text("TOKEN", encoding="utf-8")
    large = tmp_path / "src" / "large.log"
    large.write_text("x" * 2_100_000, encoding="utf-8")
    registry = build_default_registry()
    runner = AgentRunner(config=config(tmp_path), registry=registry, model_client=FakeModel([]))

    result = await runner._run_one_tool(
        ToolCall(id="call_grep", name="Grep", input={"pattern": "TOKEN", "path": "."}),
        registry,
    )

    normalized = result.content.replace("\\", "/")
    assert "src/public.txt:1:TOKEN" in normalized
    assert ".venv/secret.txt" not in result.content
    assert "[Info] Skipped large files: 1" in result.content


@pytest.mark.asyncio
async def test_task_manager_background_output(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call_task",
                        name="Agent",
                        input={
                            "description": "sub",
                            "prompt": "sub prompt",
                            "run_in_background": True,
                        },
                    )
                ]
            ),
            AssistantTurn(content="parent done"),
        ],
        prompt_routes={"sub prompt": AssistantTurn(content="sub done")},
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    await runner.run("launch sub")
    task = next(iter(runner.task_manager.tasks.values()))
    await task.handle

    assert task.status == "completed"
    assert "sub done" in task.output_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_task_manager_stop_persists_output(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path / ".ccpy" / "task-outputs")
    task = manager.create("shell work", kind="shell", owner_id="owner_1")

    message = await manager.stop(task.id, owner_id="owner_1")

    assert message == f"Stopped {task.id}"
    assert task.output_file.exists()
    assert task.output_file.read_text(encoding="utf-8") == message


@pytest.mark.asyncio
async def test_task_output_isolated_by_owner(tmp_path: Path) -> None:
    shared_manager = TaskManager(tmp_path / ".ccpy" / "task-outputs")
    registry = build_default_registry()
    runner_owner_a = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )
    runner_owner_b = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )

    task = shared_manager.create("private", kind="agent", owner_id=runner_owner_a.owner_id)
    shared_manager.complete(task.id, "secret", owner_id=runner_owner_a.owner_id)

    result = await runner_owner_b._run_one_tool(
        ToolCall(id="call_task", name="TaskOutput", input={"task_id": task.id}),
        registry,
    )

    assert result.is_error
    assert "Unknown task" in result.content


@pytest.mark.asyncio
async def test_task_stop_isolated_by_owner(tmp_path: Path) -> None:
    shared_manager = TaskManager(tmp_path / ".ccpy" / "task-outputs")
    registry = build_default_registry()
    runner_owner_a = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )
    runner_owner_b = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )

    task = shared_manager.create("private-stop", kind="agent", owner_id=runner_owner_a.owner_id)
    result = await runner_owner_b._run_one_tool(
        ToolCall(id="call_stop", name="TaskStop", input={"task_id": task.id}),
        registry,
    )

    assert result.is_error
    assert "Unknown task" in result.content
    assert task.status == "running"


@pytest.mark.asyncio
async def test_send_message_isolated_by_owner(tmp_path: Path) -> None:
    shared_manager = TaskManager(tmp_path / ".ccpy" / "task-outputs")
    registry = build_default_registry()
    runner_owner_a = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )
    runner_owner_b = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )

    task = shared_manager.create("private-msg", kind="agent", owner_id=runner_owner_a.owner_id)
    result = await runner_owner_b._run_one_tool(
        ToolCall(id="call_msg", name="SendMessage", input={"task_id": task.id, "message": "hi"}),
        registry,
    )

    assert result.is_error
    assert "Unknown task" in result.content
    assert task.pending_messages == []


@pytest.mark.asyncio
async def test_task_output_allows_same_owner(tmp_path: Path) -> None:
    shared_manager = TaskManager(tmp_path / ".ccpy" / "task-outputs")
    registry = build_default_registry()
    runner_owner_a = AgentRunner(
        config=config(tmp_path),
        registry=registry,
        model_client=FakeModel([]),
        task_manager=shared_manager,
    )

    task = shared_manager.create("shared", kind="agent", owner_id=runner_owner_a.owner_id)
    shared_manager.complete(task.id, "visible", owner_id=runner_owner_a.owner_id)
    result = await runner_owner_a._run_one_tool(
        ToolCall(id="call_task", name="TaskOutput", input={"task_id": task.id}),
        registry,
    )

    assert not result.is_error
    assert "status=completed" in result.content
    assert "visible" in result.content


@pytest.mark.asyncio
async def test_subagent_tool_counts_are_tracked(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    registry = build_default_registry()
    runner = AgentRunner(config=config(tmp_path), registry=registry, model_client=FakeModel([]))
    sub_runner = runner.clone_for_subagent(registry)

    await sub_runner._run_one_tool(ToolCall(id="call_read", name="Read", input={"path": "hello.txt"}), registry)

    assert runner.tool_stats.counts["Read"] == 1
    assert runner.tool_stats.subagent_counts["Read"] == 1


def test_openai_tool_schema_is_json_serializable() -> None:
    schemas = build_default_registry().schemas()
    assert json.dumps(schemas)
    assert any(schema["function"]["name"] == "Agent" for schema in schemas)


def test_project_memory_loads_claude_and_ccpy_memory(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("Always run tests.", encoding="utf-8")
    (tmp_path / ".ccpy").mkdir()
    (tmp_path / ".ccpy" / "memory.md").write_text("Project prefers pytest.", encoding="utf-8")

    memory = load_project_memory(tmp_path)

    assert "CLAUDE.md" in memory
    assert "Always run tests." in memory
    assert ".ccpy/memory.md" in memory
    assert "Project prefers pytest." in memory


@pytest.mark.asyncio
async def test_runner_injects_project_memory(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Use concise reports.", encoding="utf-8")
    fake = FakeModel([AssistantTurn(content="ok")])
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=fake)

    await runner.run("hello")

    assert "Use concise reports." in fake.seen_messages[0][0]["content"]


def test_model_client_parses_sse_compat_response(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        200,
        text=(
            'data: {"choices":[{"delta":{"content":"hel"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ),
    )

    body = client._parse_response_body(response)

    assert body["choices"][0]["message"]["content"] == "hello"


def test_model_client_reports_non_json_preview(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(200, text="<html>bad gateway</html>", headers={"content-type": "text/html"})

    with pytest.raises(ModelError, match="body-preview"):
        client._parse_response_body(response)


def test_model_client_suggests_v1_for_new_api_html(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        200,
        text='<!doctype html><html><head><title>New API</title></head></html>',
        headers={"content-type": "text/html; charset=utf-8"},
    )

    with pytest.raises(ModelError, match="OpenAI-compatible API root"):
        client._parse_response_body(response)


def test_model_client_builds_v1_fallback_url(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))

    assert client._fallback_v1_url("https://api.example.com") == "https://api.example.com/v1/chat/completions"
    assert client._fallback_v1_url("https://api.example.com/v1") is None


def test_model_client_adds_reasoning_content_to_assistant_tool_messages(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Glob", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "Glob", "content": "ok"},
    ]

    patched = client._add_empty_reasoning_content(messages)

    assert patched[1]["reasoning_content"] == ""
    assert "reasoning_content" not in messages[1]


def test_model_client_detects_reasoning_content_error(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        400,
        text='{"error":{"message":"thinking is enabled but reasoning_content is missing in assistant tool call message"}}',
    )

    assert client._needs_reasoning_content_compat(response)


def test_model_client_retries_rate_limit_and_parses_delay(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        429,
        text='{"error":{"message":"request reached organization max RPM: 20, please try again after 1 seconds"}}',
    )

    assert client._is_retryable_response(response)
    assert client._retry_delay(response, 0) == 1.0
    assert client._rate_limit_interval_from_response(response) == 3.25
    client._learn_rate_limit(response)
    assert client._adaptive_min_interval_s == 3.25


def test_model_client_retries_concurrency_rate_limit(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        429,
        text='{"error":{"message":"request reached max organization concurrency: 3, please try again after 1 seconds"}}',
    )

    assert client._is_retryable_response(response)
    assert client._retry_delay(response, 0) == 1.0
    assert client.config.model_max_concurrency == 1


@pytest.mark.asyncio
async def test_model_client_retries_read_timeout(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    http_client = FlakyHttpClient()

    response = await client._post_with_retries(http_client, "https://example.com/v1/chat/completions", {}, {})

    assert http_client.calls == 2
    assert response.status_code == 200


def test_model_client_does_not_retry_model_not_found(tmp_path: Path) -> None:
    client = OpenAICompatibleClient(config(tmp_path))
    response = httpx.Response(
        503,
        text='{"error":{"code":"model_not_found","message":"No available channel"}}',
    )

    assert not client._is_retryable_response(response)


def test_cli_exposes_demo_task_command() -> None:
    result = CliRunner().invoke(app, ["demo", "--help"])

    assert result.exit_code == 0
    assert "task" in result.output
    assert "autonomous" in result.output


def test_tool_call_names_extracts_assistant_function_calls() -> None:
    names = tool_call_names(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "type": "function", "function": {"name": "Agent", "arguments": "{}"}},
                    {"id": "2", "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
                ],
            },
        ]
    )

    assert names == ["Agent", "Bash"]


def test_normalize_tool_call_ids_binds_blank_tool_result_ids() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "", "type": "function", "function": {"name": "Agent", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "", "name": "Agent", "content": "ok"},
    ]

    normalized = normalize_tool_call_ids(messages)

    tool_call_id = normalized[0]["tool_calls"][0]["id"]
    assert tool_call_id
    assert normalized[1]["tool_call_id"] == tool_call_id


def test_normalize_tool_call_ids_strips_whitespace_ids() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "   ", "type": "function", "function": {"name": "Agent", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "   ", "name": "Agent", "content": "ok"},
    ]

    normalized = normalize_tool_call_ids(messages)

    assert normalized[0]["tool_calls"][0]["id"].startswith("toolu_")
    assert normalized[1]["tool_call_id"] == normalized[0]["tool_calls"][0]["id"]


def test_normalize_tool_call_ids_converts_orphan_blank_tool_message() -> None:
    normalized = normalize_tool_call_ids(
        [{"role": "tool", "tool_call_id": " ", "name": "Agent", "content": "orphan"}]
    )

    assert normalized[0]["role"] == "user"
    assert "orphan" in normalized[0]["content"]


def test_normalize_tool_call_ids_converts_orphan_nonmatching_tool_message() -> None:
    normalized = normalize_tool_call_ids(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "Agent", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "name": "Agent", "content": "orphan"},
        ]
    )

    assert normalized[1]["role"] == "user"
    assert "call_2" in normalized[1]["content"]


def test_provider_presets_keep_openai_compatible_default(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.provider = "kimi"
    cfg.base_url = "https://api.openai.com/v1"
    cfg.model = "kimi-k2.6"

    apply_provider_preset(cfg)

    assert PROVIDER_PRESETS["kimi"].base_url == "https://api.moonshot.cn/v1"
    assert cfg.base_url == "https://api.moonshot.cn/v1"
    assert isinstance(create_provider(cfg), OpenAICompatibleClient)


def test_micro_compact_truncates_old_tool_results() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "Read", "content": "x" * 100},
        {"role": "assistant", "content": "done"},
    ]

    compacted = micro_compact_tool_results(messages, max_chars=10)

    assert compacted[2]["content"] == "x" * 100


def test_compact_messages_uses_session_summary_and_keeps_budget() -> None:
    messages = [{"role": "system", "content": "s"}]
    for idx in range(40):
        messages.append({"role": "user", "content": f"message {idx} " + "x" * 400})

    compacted = compact_messages(messages, max_tokens=300, recent_messages=6, session_summary="important session goal")

    assert "important session goal" in compacted[1]["content"]
    assert len(compacted) <= 8
    assert estimate_tokens_messages(compacted) < estimate_tokens_messages(messages)


def test_memory_scans_frontmatter_manifest(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".ccpy" / "memories"
    memory_dir.mkdir(parents=True)
    (memory_dir / "release.md").write_text("---\ndescription: Release checklist\n---\n# Release\n", encoding="utf-8")

    headers = scan_memory_files(memory_dir, workspace=tmp_path)
    memory = load_project_memory(tmp_path)

    assert headers[0].description == "Release checklist"
    assert ".ccpy/memories/manifest" in memory


def test_session_notes_store_writes_summary(tmp_path: Path) -> None:
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=FakeModel([]))
    notes = SessionNotesStore(runner.session_store)

    notes.update(
        [
            {"role": "user", "content": "build feature"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "Bash", "arguments": "{}"}}],
            },
        ]
    )

    summary = notes.summary()
    assert "build feature" in summary
    assert "Bash" in summary


@pytest.mark.asyncio
async def test_hook_manager_blocks_tool_execution(tmp_path: Path) -> None:
    async def block_bash(context):
        if context.tool_name == "Bash":
            return HookResult(blocking_error="blocked by hook")
        return None

    hooks = HookManager()
    hooks.register(HookEvent.PRE_TOOL_USE, block_bash)
    runner = AgentRunner(
        config=config(tmp_path),
        registry=build_default_registry(),
        model_client=FakeModel([]),
        hook_manager=hooks,
    )

    result = await runner._run_one_tool(ToolCall(id="call_1", name="Bash", input={"command": "echo hi"}), runner.registry)

    assert result.is_error
    assert result.content == "blocked by hook"


@pytest.mark.asyncio
async def test_mcp_bridge_tool_wraps_session_result(tmp_path: Path) -> None:
    class Block:
        text = "hello from mcp"

    class Result:
        content = [Block()]

    class Session:
        async def call_tool(self, name, arguments=None):
            assert name == "echo"
            assert arguments == {"value": "x"}
            return Result()

    tool = MCPBridgeTool(
        server_name="demo",
        raw_name="echo",
        description="Echo",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        session=Session(),
    )
    runner = AgentRunner(config=config(tmp_path), registry=build_default_registry(), model_client=FakeModel([]))

    result = await tool.execute("call_mcp", {"value": "x"}, runner.context())

    assert result.content == "hello from mcp"
    assert tool.name == "mcp__demo__echo"


def test_mcp_manager_collects_server_instructions() -> None:
    class ServerInfo:
        instructions = "Use carefully"

    class Session:
        server_info = ServerInfo()

    manager = MCPManager()
    manager._extract_server_instructions("demo", Session())

    assert "Use carefully" in manager.build_instructions_prompt()
