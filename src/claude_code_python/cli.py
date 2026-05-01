from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .config import Config
from .mcp import load_mcp_http_tools, load_mcp_servers
from .runner import AgentRunner
from .session import JSONLSessionStore, SessionManager
from .tools.default import build_default_registry

app = typer.Typer(help="Claude-Code-Python: a Python coding agent with tools and Task sub-agents.")
demo_app = typer.Typer(help="Run built-in demos.")
app.add_typer(demo_app, name="demo")
console = Console()
ROOT_COMMANDS = {"demo", "run", "sessions", "resume"}


def make_runner(workspace: Optional[Path] = None, session_id: str | None = None) -> AgentRunner:
    config = Config.from_env(workspace=workspace)
    registry = build_default_registry()
    session_store = JSONLSessionStore(config.session_dir, session_id=session_id) if session_id else None
    return AgentRunner(config=config, registry=registry, session_store=session_store)


def tool_call_names(messages: list[dict]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name")
            if name:
                names.append(name)
    return names


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))


def print_tool_summary(messages: list[dict], tool_counts: dict[str, int] | None = None, subagent_counts: dict[str, int] | None = None) -> None:
    names = tool_call_names(messages)
    if not names:
        console.print("\n[yellow]Tool calls:[/yellow] none")
    else:
        counts = {name: names.count(name) for name in sorted(set(names))}
        autonomous_agent = any(name in {"Agent", "Task"} for name in names)
        console.print(f"\n[cyan]Top-level tool calls:[/cyan] {format_counts(counts)}")
        console.print(f"[cyan]Autonomous Agent/Task used:[/cyan] {autonomous_agent}")

    if tool_counts:
        console.print(f"[cyan]Global executed tools:[/cyan] {format_counts(tool_counts)}")
    if subagent_counts:
        console.print(f"[cyan]Sub-agent executed tools:[/cyan] {format_counts(subagent_counts)}")


async def _load_mcp_for_runner(
    runner: AgentRunner,
    mcp_manifest: Optional[str] = None,
    mcp_config: Optional[Path] = None,
):
    mcp_manager = None
    if mcp_manifest:
        await load_mcp_http_tools(runner.registry, mcp_manifest)
    mcp_servers = list(runner.config.mcp_servers)
    if mcp_config:
        mcp_servers.extend(_load_mcp_config_file(mcp_config))
    if mcp_servers:
        mcp_manager = await load_mcp_servers(runner.registry, mcp_servers)
    return mcp_manager


def render_sessions(manager: SessionManager) -> None:
    sessions = manager.list_sessions()
    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return
    table = Table(title="CCPY Sessions")
    table.add_column("Session")
    table.add_column("Events", justify="right")
    table.add_column("Updated")
    table.add_column("Path")
    for session in sessions:
        table.add_row(session.session_id, str(session.events), session.updated_at, str(session.path))
    console.print(table)


@app.callback()
def callback() -> None:
    """Claude-Code-Python command line."""


@app.command("run")
def run_prompt(
    prompt: str = typer.Argument(..., help="Task prompt to run."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="Workspace root."),
    mcp_manifest: Optional[str] = typer.Option(None, "--mcp-manifest", help="Optional HTTP MCP-like manifest URL."),
    mcp_config: Optional[Path] = typer.Option(None, "--mcp-config", help="Optional MCP server config JSON file."),
) -> None:
    async def run() -> None:
        runner = make_runner(workspace)
        mcp_manager = None
        try:
            mcp_manager = await _load_mcp_for_runner(runner, mcp_manifest, mcp_config)
            result = await runner.run(prompt)
            console.print(Markdown(result.text or ""))
            print_tool_summary(result.messages, result.tool_counts, result.subagent_tool_counts)
            console.print(f"\n[dim]turns={result.turns} session={runner.session_store.path}[/dim]")
        finally:
            if mcp_manager:
                await mcp_manager.shutdown()

    asyncio.run(run())


@app.command("sessions")
def sessions_command(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="Workspace root.")) -> None:
    """List saved interactive sessions."""
    config = Config.from_env(workspace=workspace)
    render_sessions(SessionManager(config.session_dir))


@app.command("resume")
def resume_command(
    session_id: str = typer.Argument(..., help="Session id, for example sess_xxx."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w", help="Workspace root."),
    mcp_manifest: Optional[str] = typer.Option(None, "--mcp-manifest", help="Optional HTTP MCP-like manifest URL."),
    mcp_config: Optional[Path] = typer.Option(None, "--mcp-config", help="Optional MCP server config JSON file."),
) -> None:
    """Resume a saved session in interactive mode."""
    asyncio.run(interactive(workspace=workspace, session_id=session_id, mcp_manifest=mcp_manifest, mcp_config=mcp_config))


def main() -> None:
    """Dispatch `ccpy "prompt"` to `ccpy run "prompt"` while preserving subcommands."""
    args = sys.argv[1:]
    if args and not args[0].startswith("-") and args[0] not in ROOT_COMMANDS:
        sys.argv.insert(1, "run")
    app()


def _load_mcp_config_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("servers"), list):
        return [item for item in data["servers"] if isinstance(item, dict)]
    raise typer.BadParameter("MCP config must be a JSON list or an object with a 'servers' list.")


async def interactive(
    workspace: Optional[Path] = None,
    session_id: str | None = None,
    mcp_manifest: Optional[str] = None,
    mcp_config: Optional[Path] = None,
) -> None:
    runner = make_runner(workspace, session_id=session_id)
    manager = SessionManager(runner.config.session_dir)
    if session_id and not manager.exists(session_id):
        console.print(f"[yellow]Session {session_id!r} not found. Creating it.[/yellow]")
    messages = _load_interactive_messages(runner, manager, session_id)
    mcp_manager = None
    try:
        mcp_manager = await _load_mcp_for_runner(runner, mcp_manifest, mcp_config)
        _print_interactive_banner(runner)
        while True:
            try:
                user_input = console.input("[bold green]ccpy> [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Bye.[/dim]")
                return
            if not user_input:
                continue
            command_result = _handle_repl_command(user_input, runner, manager, messages)
            if command_result == "exit":
                return
            if isinstance(command_result, list):
                messages = command_result
                continue
            try:
                result = await runner.continue_run(messages, user_input)
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")
                continue
            messages = result.messages
            console.print(Markdown(result.text or ""))
            print_tool_summary(result.messages, result.tool_counts, result.subagent_tool_counts)
    finally:
        if mcp_manager:
            await mcp_manager.shutdown()


def _load_interactive_messages(
    runner: AgentRunner,
    manager: SessionManager,
    session_id: str | None,
) -> list[dict]:
    if session_id and manager.exists(session_id):
        restored = manager.load_session_messages(session_id)
        return _with_system_message(runner, restored)
    return runner.initial_messages()


def _with_system_message(runner: AgentRunner, messages: list[dict]) -> list[dict]:
    if messages and messages[0].get("role") == "system":
        return messages
    return [runner.initial_messages()[0], *messages]


def _print_interactive_banner(runner: AgentRunner) -> None:
    console.print(
        Panel.fit(
            "\n".join(
                [
                    "[bold cyan]Claude-Code-Python interactive session[/bold cyan]",
                    f"workspace: {runner.config.workspace}",
                    f"session: {runner.session_store.session_id}",
                    f"model: {runner.config.model}",
                    "type /help for commands, /exit to quit",
                ]
            ),
            border_style="cyan",
        )
    )


def _handle_repl_command(
    user_input: str,
    runner: AgentRunner,
    manager: SessionManager,
    messages: list[dict],
) -> str | list[dict] | None:
    if not user_input.startswith("/"):
        return None
    command, _, rest = user_input.partition(" ")
    command = command.lower()
    arg = rest.strip()
    if command in {"/exit", "/quit"}:
        console.print("[dim]Bye.[/dim]")
        return "exit"
    if command == "/help":
        console.print(
            "Commands: /help, /new, /sessions, /resume <session_id>, "
            "/status, /clear, /exit"
        )
        return messages
    if command == "/new":
        new_store = manager.create()
        runner.session_store = new_store
        runner.session_notes = type(runner.session_notes)(new_store) if runner.session_notes else None
        console.print(f"[cyan]New session:[/cyan] {new_store.session_id}")
        return runner.initial_messages()
    if command == "/sessions":
        render_sessions(manager)
        return messages
    if command == "/resume":
        if not arg:
            console.print("[red]Usage:[/red] /resume <session_id>")
            return messages
        if not manager.exists(arg):
            console.print(f"[red]Unknown session:[/red] {arg}")
            return messages
        runner.session_store = manager.open(arg)
        runner.session_notes = type(runner.session_notes)(runner.session_store) if runner.session_notes else None
        restored = _with_system_message(runner, runner.session_store.load_messages())
        console.print(f"[cyan]Resumed session:[/cyan] {arg}")
        return restored
    if command == "/status":
        console.print(
            "\n".join(
                [
                    f"workspace: {runner.config.workspace}",
                    f"session: {runner.session_store.session_id}",
                    f"model: {runner.config.model}",
                    f"messages: {len(messages)}",
                    f"tools: {len(runner.registry.unique_tools())}",
                    f"tool_counts: {format_counts(runner.tool_stats.counts) if runner.tool_stats.counts else '(none)'}",
                ]
            )
        )
        return messages
    if command == "/clear":
        console.print("[cyan]Cleared in-memory context for this session.[/cyan]")
        return runner.initial_messages()
    console.print(f"[red]Unknown command:[/red] {command}. Type /help.")
    return messages


@demo_app.command("task")
def demo_task(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")) -> None:
    """Show how the Task/Agent tool can fan out parallel sub-agents."""

    async def run() -> None:
        runner = make_runner(workspace)
        prompt = """
Use the Agent tool to run three independent sub-agents in the same turn:
1. A researcher that inspects the workspace with Glob/Read.
2. A tester that runs a harmless Bash command to list Python files.
3. A reviewer that proposes risks and next tests.
Then combine their results into a concise report.
"""
        result = await runner.run(prompt)
        console.print(Markdown(result.text or ""))
        print_tool_summary(result.messages, result.tool_counts, result.subagent_tool_counts)

    asyncio.run(run())


@demo_app.command("autonomous")
def demo_autonomous(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")) -> None:
    """Check whether the main agent chooses Agent/Task for a generic complex task."""

    async def run() -> None:
        runner = make_runner(workspace)
        prompt = """
Analyze this repository as if preparing it for a public GitHub release.
Inspect the project structure, run appropriate local checks, identify risks, and produce a concise release-readiness report.
Use whatever tools are useful for the work.
"""
        result = await runner.run(prompt)
        console.print(Markdown(result.text or ""))
        print_tool_summary(result.messages, result.tool_counts, result.subagent_tool_counts)
        console.print(
            "\n[dim]This demo does not explicitly ask for Agent/Task. "
            "If Autonomous Agent/Task used=True, the model chose sub-agents for the generic task.[/dim]"
        )

    asyncio.run(run())


@demo_app.command("multi-agent")
def demo_multi_agent(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")) -> None:
    """Show a coordinator/worker style multi-agent system."""

    async def run() -> None:
        runner = make_runner(workspace)
        prompt = """
You are a coordinator testing a multi-agent coding system.
Do not answer from memory. First, call the Agent tool exactly three times in the same assistant turn:
1. description="coder", subagent_type="coder", run_in_background=false, prompt="Inspect src/claude_code_python and propose one small implementation improvement. Use Glob/Read if useful."
2. description="tester", subagent_type="tester", run_in_background=false, prompt="Inspect tests and propose one focused test improvement. Use Glob/Read/Bash if useful."
3. description="reviewer", subagent_type="reviewer", run_in_background=false, prompt="Review README.md and VALIDATION_REPORT.md for release-readiness risks. Use Read if useful."
After all three Agent tool results are returned, combine them into a concise multi-agent report.
If you cannot call tools, say exactly: TOOL_CALLING_NOT_AVAILABLE.
"""
        result = await runner.run(prompt)
        console.print(Markdown(result.text or ""))
        print_tool_summary(result.messages, result.tool_counts, result.subagent_tool_counts)

    asyncio.run(run())


if __name__ == "__main__":
    main()
