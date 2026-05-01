from __future__ import annotations

import json
from typing import Any


DEFAULT_MAX_TOKENS = 20_000
DEFAULT_RECENT_MESSAGES = 24
DEFAULT_TOOL_RESULT_MAX_CHARS = 8_000


def estimate_tokens_text(text: str) -> int:
    """Small tokenizer-free estimate used for local compaction decisions."""
    return max(1, len(text) // 4)


def estimate_tokens_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens_text(json.dumps(message, ensure_ascii=False, default=str)) for message in messages)


def compact_messages(
    messages: list[dict[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    recent_messages: int = DEFAULT_RECENT_MESSAGES,
    tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
    session_summary: str | None = None,
) -> list[dict[str, Any]]:
    """Apply Claw-inspired micro + snip compaction without another model call.

    The implementation deliberately preserves OpenAI tool-call invariants by
    keeping a contiguous recent tail and converting leading orphan tool results
    into user-visible diagnostics.
    """
    compacted = micro_compact_tool_results(messages, max_chars=tool_result_max_chars)
    if estimate_tokens_messages(compacted) <= max_tokens or len(compacted) < 4:
        return compacted

    system_messages = [message for message in compacted if message.get("role") == "system"][:1]
    non_system = [message for message in compacted if message.get("role") != "system"]
    tail = non_system[-recent_messages:]
    tail = _convert_leading_orphan_tools(tail)
    summary_text = session_summary or (
        "Earlier conversation history was compacted by Claude-Code-Python. "
        "Continue using the visible recent context and preserve the user's latest goal."
    )
    summary = {"role": "user", "content": f"[Compacted context summary]\n{summary_text}"}
    return [*system_messages, summary, *tail]


def micro_compact_tool_results(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    if max_chars <= 0:
        return messages
    tool_indexes = [idx for idx, message in enumerate(messages) if message.get("role") == "tool"]
    keep_intact = set(tool_indexes[-5:])
    output: list[dict[str, Any]] = []
    for idx, message in enumerate(messages):
        item = dict(message)
        content = item.get("content")
        if (
            idx not in keep_intact
            and item.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > max_chars
        ):
            item["content"] = (
                content[:max_chars]
                + f"\n\n[Micro compacted: removed {len(content) - max_chars} older characters from this tool result.]"
            )
        output.append(item)
    return output


def reactive_compact_messages(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    session_summary: str | None = None,
) -> list[dict[str, Any]]:
    return compact_messages(
        messages,
        max_tokens=max(1, max_tokens // 2),
        recent_messages=max(8, DEFAULT_RECENT_MESSAGES // 2),
        tool_result_max_chars=max(1_000, DEFAULT_TOOL_RESULT_MAX_CHARS // 2),
        session_summary=session_summary,
    )


def _convert_leading_orphan_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = [dict(message) for message in messages]
    while output and output[0].get("role") == "tool":
        orphan = output.pop(0)
        output.insert(
            0,
            {
                "role": "user",
                "content": (
                    "A tool result was retained after context compaction without its "
                    f"assistant tool call (tool_call_id={orphan.get('tool_call_id', '<missing>')}):\n"
                    f"{orphan.get('content', '')}"
                ),
            },
        )
        break
    return output
