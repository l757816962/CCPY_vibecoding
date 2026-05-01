from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        if not self.id:
            self.id = new_id("toolu")


@dataclass(slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    data: Any = None

    def to_openai_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "content": self.content,
        }


@dataclass(slots=True)
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_openai_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    function = raw.get("function") or {}
    args = function.get("arguments") or "{}"
    if isinstance(args, str):
        try:
            payload = json.loads(args)
        except json.JSONDecodeError:
            payload = {"_raw": args}
    elif isinstance(args, dict):
        payload = args
    else:
        payload = {"_raw": args}
    return ToolCall(
        id=raw.get("id") or new_id("toolu"),
        name=function.get("name") or raw.get("name") or "",
        input=payload,
    )


def normalize_tool_call_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure assistant tool calls and following tool results share non-empty ids."""
    normalized: list[dict[str, Any]] = []
    pending_tool_ids: list[str] = []

    for message in messages:
        item = dict(message)
        if item.get("role") == "assistant" and item.get("tool_calls"):
            tool_calls: list[dict[str, Any]] = []
            pending_tool_ids = []
            for raw_call in item.get("tool_calls") or []:
                call = dict(raw_call)
                call["id"] = str(call.get("id") or "").strip()
                if not call["id"]:
                    call["id"] = new_id("toolu")
                pending_tool_ids.append(call["id"])
                tool_calls.append(call)
            item["tool_calls"] = tool_calls
        elif item.get("role") == "tool":
            current_id = str(item.get("tool_call_id") or "").strip()
            if not current_id and pending_tool_ids:
                current_id = pending_tool_ids.pop(0)
                item["tool_call_id"] = current_id
            elif current_id in pending_tool_ids:
                pending_tool_ids.remove(current_id)
                item["tool_call_id"] = current_id
            else:
                item = {
                    "role": "user",
                    "content": (
                        "Tool result without a matching preceding assistant tool call "
                        f"(tool_call_id={current_id or '<empty>'}):\n{item.get('content', '')}"
                    ),
                }
        elif item.get("role") not in {"tool"}:
            pending_tool_ids = []
        normalized.append(item)

    return normalized
