from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ..messages import ToolResult


class ToolContext(Protocol):
    workspace: Any
    config: Any
    permissions: Any
    runner: Any
    task_manager: Any
    todos: list[dict[str, Any]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    aliases: tuple[str, ...] = ()

    def openai_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class BaseTool:
    spec: ToolSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.spec.aliases

    def validate(self, payload: dict[str, Any]) -> BaseModel:
        try:
            return self.spec.input_model.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(exc.errors(include_url=False)) from exc

    async def call(self, payload: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError

    async def execute(self, tool_call_id: str, raw_input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            payload = self.validate(raw_input)
            return await self.call(payload, context)
        except Exception as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=f"Tool {self.name} failed: {exc}",
                is_error=True,
            )


def json_result(tool: str, tool_call_id: str, data: Any, is_error: bool = False) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        name=tool,
        content=json.dumps(data, ensure_ascii=False, indent=2),
        is_error=is_error,
        data=data,
    )
