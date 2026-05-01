from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, create_model

from .messages import ToolResult
from .tools.base import BaseTool, ToolContext, ToolSpec
from .tools.registry import ToolRegistry

MAX_MCP_DESCRIPTION_LENGTH = 2048


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _truncate_description(description: str) -> str:
    if len(description) <= MAX_MCP_DESCRIPTION_LENGTH:
        return description
    return description[:MAX_MCP_DESCRIPTION_LENGTH] + "... [truncated]"


def _schema_model(name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for key in (input_schema.get("properties") or {}).keys():
        required = key in (input_schema.get("required") or [])
        fields[key] = (Any, ... if required else None)
    return create_model(f"{name}Input", **fields) if fields else create_model(f"{name}Input")


class MCPHttpTool(BaseTool):
    def __init__(self, name: str, description: str, input_schema: dict[str, Any], endpoint: str):
        model = _schema_model(name, input_schema)
        self.spec = ToolSpec(name=name, description=description, input_model=model, is_concurrency_safe=True)
        self.endpoint = endpoint

    async def call(self, payload: BaseModel, context: ToolContext) -> ToolResult:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(self.endpoint, json={"name": self.name, "arguments": payload.model_dump()})
        response.raise_for_status()
        data = response.json()
        return ToolResult("", self.name, json.dumps(data, ensure_ascii=False, indent=2), data=data)


class MCPBridgeTool(BaseTool):
    def __init__(self, server_name: str, raw_name: str, description: str, input_schema: dict[str, Any], session: Any):
        name = f"mcp__{server_name}__{raw_name}"
        self.raw_name = raw_name
        self.session = session
        self.spec = ToolSpec(
            name=name,
            description=_truncate_description(description or "MCP tool"),
            input_model=_schema_model(name, input_schema),
            is_concurrency_safe=True,
        )

    async def call(self, payload: BaseModel, context: ToolContext) -> ToolResult:
        result = await self.session.call_tool(self.raw_name, arguments=payload.model_dump())
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            parts.append(getattr(block, "text", str(block)))
        return ToolResult("", self.name, "\n".join(parts) if parts else "(empty MCP result)", data=result)


class MCPManager:
    def __init__(self):
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._server_instructions: dict[str, str] = {}

    async def connect_all(self, servers: list[MCPServerConfig]) -> None:
        for server in servers:
            await self.connect(server)

    async def connect(self, server: MCPServerConfig) -> Any:
        transport = server.transport.lower()
        if transport == "stdio":
            session = await self._connect_stdio(server)
        elif transport == "sse":
            session = await self._connect_sse(server)
        elif transport == "http":
            session = await self._connect_http(server)
        else:
            raise ValueError(f"Unsupported MCP transport: {server.transport}")
        self._sessions[server.name] = session
        self._extract_server_instructions(server.name, session)
        return session

    async def _connect_stdio(self, server: MCPServerConfig) -> Any:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("Install the optional 'mcp' package to use MCP stdio servers.") from exc
        params = StdioServerParameters(command=server.command, args=server.args, env=server.env or None)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _connect_sse(self, server: MCPServerConfig) -> Any:
        if not server.url:
            raise ValueError(f"MCP server {server.name!r} requires url for sse transport")
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as exc:
            raise RuntimeError("Install the optional 'mcp' package to use MCP SSE servers.") from exc
        read, write = await self._stack.enter_async_context(sse_client(server.url, headers=server.headers or None))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _connect_http(self, server: MCPServerConfig) -> Any:
        if not server.url:
            raise ValueError(f"MCP server {server.name!r} requires url for http transport")
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError("Install the optional 'mcp' package to use MCP HTTP servers.") from exc
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(server.url, headers=server.headers or None)
        )
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def discover_tools(self, registry: ToolRegistry) -> None:
        for server_name, session in self._sessions.items():
            tools_response = await session.list_tools()
            for tool_def in getattr(tools_response, "tools", []) or []:
                raw_name = _get_attr(tool_def, "name", "")
                if not raw_name:
                    continue
                name = f"mcp__{server_name}__{raw_name}"
                if registry.get(name):
                    continue
                registry.register(
                    MCPBridgeTool(
                        server_name=server_name,
                        raw_name=raw_name,
                        description=_get_attr(tool_def, "description", "MCP tool"),
                        input_schema=_get_attr(tool_def, "inputSchema", {}) or _get_attr(tool_def, "input_schema", {}),
                        session=session,
                    )
                )

    def build_instructions_prompt(self) -> str:
        if not self._server_instructions:
            return ""
        sections = [
            f"## MCP server: {name}\n{instructions}"
            for name, instructions in sorted(self._server_instructions.items())
        ]
        return "# MCP Server Instructions\n" + "\n\n".join(sections)

    async def shutdown(self) -> None:
        await self._stack.aclose()

    def _extract_server_instructions(self, name: str, session: Any) -> None:
        server_info = getattr(session, "server_info", None)
        instructions = getattr(server_info, "instructions", None) if server_info else None
        if instructions:
            self._server_instructions[name] = _truncate_description(str(instructions))


async def load_mcp_http_tools(registry: ToolRegistry, manifest_url: str, call_url: str | None = None) -> None:
    """Load a small HTTP MCP-like manifest.

    Expected manifest shape:
    {"tools": [{"name": "...", "description": "...", "inputSchema": {...}}]}
    Calls are POSTed to `call_url or manifest_url + "/call"`.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(manifest_url)
    response.raise_for_status()
    endpoint = call_url or manifest_url.rstrip("/") + "/call"
    for item in response.json().get("tools", []):
        if registry.get(item["name"]):
            continue
        registry.register(
            MCPHttpTool(
                name=item["name"],
                description=item.get("description", "MCP tool"),
                input_schema=item.get("inputSchema", {}),
                endpoint=endpoint,
            )
        )


async def load_mcp_servers(registry: ToolRegistry, configs: list[dict[str, Any]]) -> MCPManager:
    manager = MCPManager()
    servers = [MCPServerConfig(**config) for config in configs]
    await manager.connect_all(servers)
    await manager.discover_tools(registry)
    return manager


def _get_attr(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)
