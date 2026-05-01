from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .messages import new_id


@dataclass(slots=True)
class ManagedTask:
    id: str
    owner_id: str
    description: str
    kind: str
    output_file: Path
    status: str = "running"
    result: str = ""
    error: str | None = None
    pending_messages: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    handle: asyncio.Task | asyncio.subprocess.Process | None = None


class TaskManager:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, ManagedTask] = {}

    def create(self, description: str, kind: str = "agent", owner_id: str = "public") -> ManagedTask:
        task_id = new_id("task")
        task = ManagedTask(
            id=task_id,
            owner_id=owner_id,
            description=description,
            kind=kind,
            output_file=self.output_dir / f"{task_id}.txt",
        )
        self.tasks[task_id] = task
        return task

    def get(self, task_id: str, owner_id: str | None = None) -> ManagedTask | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if owner_id is not None and task.owner_id != owner_id:
            return None
        return task

    def queue_message(self, task_id: str, message: str, owner_id: str | None = None) -> None:
        task = self.require(task_id, owner_id=owner_id)
        task.pending_messages.append(message)

    def drain_messages(self, task_id: str, owner_id: str | None = None) -> list[str]:
        task = self.require(task_id, owner_id=owner_id)
        messages = list(task.pending_messages)
        task.pending_messages.clear()
        return messages

    def complete(self, task_id: str, result: str, status: str = "completed", owner_id: str | None = None) -> None:
        task = self.require(task_id, owner_id=owner_id)
        task.status = status
        task.result = result
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.output_file.write_text(result, encoding="utf-8")

    def fail(self, task_id: str, error: str, owner_id: str | None = None) -> None:
        task = self.require(task_id, owner_id=owner_id)
        task.status = "failed"
        task.error = error
        task.result = error
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.output_file.write_text(error, encoding="utf-8")

    async def stop(self, task_id: str, owner_id: str | None = None) -> str:
        task = self.require(task_id, owner_id=owner_id)
        if task.status != "running":
            return f"Task {task_id} already {task.status}"
        if isinstance(task.handle, asyncio.Task):
            task.handle.cancel()
        elif task.handle is not None:
            task.handle.terminate()
        task.status = "killed"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.result = f"Stopped {task_id}"
        task.output_file.write_text(task.result, encoding="utf-8")
        return task.result

    def require(self, task_id: str, owner_id: str | None = None) -> ManagedTask:
        task = self.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        return task
