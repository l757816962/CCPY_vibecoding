from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .messages import new_id


@dataclass(slots=True)
class SessionEvent:
    uuid: str
    parent_uuid: str | None
    role: str
    content: Any
    created_at: str


class JSONLSessionStore:
    def __init__(self, root: Path, session_id: str | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or new_id("sess")
        self.path = self.root / f"{self.session_id}.jsonl"
        self._last_uuid: str | None = None

    def record(self, role: str, content: Any) -> str:
        event_id = new_id("evt")
        event = SessionEvent(
            uuid=event_id,
            parent_uuid=self._last_uuid,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        self._last_uuid = event_id
        return event_id

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def load_messages(self) -> list[dict[str, Any]]:
        return events_to_messages(self.load())


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    path: Path
    updated_at: str
    events: int


class SessionManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> JSONLSessionStore:
        return JSONLSessionStore(self.root)

    def open(self, session_id: str) -> JSONLSessionStore:
        return JSONLSessionStore(self.root, session_id=session_id)

    def exists(self, session_id: str) -> bool:
        return (self.root / f"{session_id}.jsonl").is_file()

    def list_sessions(self) -> list[SessionInfo]:
        sessions: list[SessionInfo] = []
        for path in sorted(self.root.glob("sess_*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                events = len([line for line in path.read_text(encoding="utf-8").splitlines() if line])
            except OSError:
                events = 0
            sessions.append(
                SessionInfo(
                    session_id=path.stem,
                    path=path,
                    updated_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    events=events,
                )
            )
        return sessions

    def load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.open(session_id).load_messages()


def events_to_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in events:
        role = event.get("role")
        content = event.get("content")
        message = _event_to_message(role, content)
        if message:
            messages.append(message)
    return messages


def _event_to_message(role: str, content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict) and isinstance(content.get("role"), str):
        if content["role"] in {"system", "user", "assistant", "tool"}:
            return dict(content)
    if role in {"system", "user", "assistant"}:
        return {"role": role, "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}
    if role == "tool":
        return {
            "role": "user",
            "content": "Recovered tool event without full tool message:\n"
            + (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)),
        }
    return None


class SessionNotesStore:
    """Small structured notes file used as a local compact summary source."""

    def __init__(self, session_store: JSONLSessionStore):
        self.session_store = session_store
        self.path = session_store.root / f"{session_store.session_id}.notes.md"

    def update(self, messages: list[dict[str, Any]]) -> None:
        self.session_store.root.mkdir(parents=True, exist_ok=True)
        recent_user = _last_content(messages, "user")
        recent_assistant = _last_content(messages, "assistant")
        tool_names = _tool_names(messages)
        content = [
            "# Session Notes",
            "",
            f"- Session: `{self.session_store.session_id}`",
            f"- Latest user request: {recent_user or '(none)'}",
            f"- Latest assistant response: {recent_assistant or '(none)'}",
            f"- Tools observed: {', '.join(tool_names) if tool_names else '(none)'}",
            "",
            "These notes are maintained automatically to preserve task intent during context compaction.",
        ]
        self.path.write_text("\n".join(content), encoding="utf-8")

    def summary(self, max_chars: int = 8_000) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def _last_content(messages: list[dict[str, Any]], role: str) -> str:
    for message in reversed(messages):
        if message.get("role") == role:
            content = message.get("content")
            if isinstance(content, str):
                return content[:500]
            if content is not None:
                return str(content)[:500]
    return ""


def _tool_names(messages: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names
