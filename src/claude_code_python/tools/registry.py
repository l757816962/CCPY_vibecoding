from __future__ import annotations

from collections.abc import Iterable

from .base import BaseTool


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()):
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        for alias in tool.aliases:
            self._tools[alias] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schemas(self, allowed_tools: set[str] | None = None) -> list[dict]:
        seen: set[str] = set()
        schemas: list[dict] = []
        for tool in self._tools.values():
            if tool.name in seen:
                continue
            if allowed_tools is not None and tool.name not in allowed_tools:
                continue
            seen.add(tool.name)
            schemas.append(tool.spec.openai_schema())
        return schemas

    def filter(self, denied: set[str] = frozenset(), allowed: set[str] | None = None) -> "ToolRegistry":
        output = ToolRegistry()
        for tool in self.unique_tools():
            if tool.name in denied:
                continue
            if allowed is not None and tool.name not in allowed:
                continue
            output.register(tool)
        return output

    def unique_tools(self) -> list[BaseTool]:
        seen: set[str] = set()
        tools: list[BaseTool] = []
        for tool in self._tools.values():
            if tool.name not in seen:
                seen.add(tool.name)
                tools.append(tool)
        return tools
