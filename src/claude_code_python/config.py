from __future__ import annotations

import os
import sys
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Config:
    provider: str = "openai-compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "k2.6"
    workspace: Path = Path.cwd()
    max_tool_concurrency: int = 10
    max_turns: int = 20
    request_timeout_s: float = 500.0
    model_max_concurrency: int = 1
    model_min_interval_s: float = 3.5
    model_max_retries: int = 6
    model_retry_base_delay_s: float = 1.0
    model_retry_max_delay_s: float = 30.0
    project_memory_enabled: bool = True
    project_memory_max_chars: int = 20_000
    memory_index_enabled: bool = True
    session_notes_enabled: bool = True
    compact_max_tokens: int = 20_000
    compact_recent_messages: int = 24
    compact_tool_result_max_chars: int = 8_000
    compact_reactive_enabled: bool = True
    hooks_enabled: bool = True
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    session_dir: Path = Path(".ccpy/sessions")
    task_output_dir: Path = Path(".ccpy/task-outputs")
    permission_mode: str = "ask"
    shell: str | None = None

    @classmethod
    def from_env(cls, workspace: str | Path | None = None) -> "Config":
        root = Path(workspace or os.getenv("CCPY_WORKSPACE") or Path.cwd()).resolve()
        return cls(
            provider=os.getenv("CCPY_PROVIDER", "openai-compatible"),
            base_url=os.getenv("CCPY_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("CCPY_API_KEY") or os.getenv("OPENAI_API_KEY"),
            model=os.getenv("CCPY_MODEL", "kimi-k2.6"),
            workspace=root,
            max_tool_concurrency=int(os.getenv("CCPY_MAX_TOOL_CONCURRENCY", "10")),
            max_turns=int(os.getenv("CCPY_MAX_TURNS", "20")),
            request_timeout_s=float(os.getenv("CCPY_REQUEST_TIMEOUT_S", "120")),
            model_max_concurrency=int(os.getenv("CCPY_MODEL_MAX_CONCURRENCY", "1")),
            model_min_interval_s=float(os.getenv("CCPY_MODEL_MIN_INTERVAL_S", "0")),
            model_max_retries=int(os.getenv("CCPY_MODEL_MAX_RETRIES", "6")),
            model_retry_base_delay_s=float(os.getenv("CCPY_MODEL_RETRY_BASE_DELAY_S", "1")),
            model_retry_max_delay_s=float(os.getenv("CCPY_MODEL_RETRY_MAX_DELAY_S", "30")),
            project_memory_enabled=os.getenv("CCPY_PROJECT_MEMORY", "1").lower() not in {"0", "false", "no"},
            project_memory_max_chars=int(os.getenv("CCPY_PROJECT_MEMORY_MAX_CHARS", "20000")),
            memory_index_enabled=os.getenv("CCPY_MEMORY_INDEX", "1").lower() not in {"0", "false", "no"},
            session_notes_enabled=os.getenv("CCPY_SESSION_NOTES", "1").lower() not in {"0", "false", "no"},
            compact_max_tokens=int(os.getenv("CCPY_COMPACT_MAX_TOKENS", "20000")),
            compact_recent_messages=int(os.getenv("CCPY_COMPACT_RECENT_MESSAGES", "24")),
            compact_tool_result_max_chars=int(os.getenv("CCPY_COMPACT_TOOL_RESULT_MAX_CHARS", "8000")),
            compact_reactive_enabled=os.getenv("CCPY_COMPACT_REACTIVE", "1").lower() not in {"0", "false", "no"},
            hooks_enabled=os.getenv("CCPY_HOOKS", "1").lower() not in {"0", "false", "no"},
            mcp_servers=_load_mcp_servers(os.getenv("CCPY_MCP_SERVERS")),
            session_dir=(root / os.getenv("CCPY_SESSION_DIR", ".ccpy/sessions")).resolve(),
            task_output_dir=(root / os.getenv("CCPY_TASK_OUTPUT_DIR", ".ccpy/task-outputs")).resolve(),
            permission_mode=os.getenv("CCPY_PERMISSION_MODE", "ask").lower(),
            shell=os.getenv("CCPY_SHELL"),
        )

    @property
    def default_shell(self) -> list[str]:
        if self.shell:
            return [self.shell]
        if sys.platform.startswith("win"):
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        return ["/bin/bash", "-lc"]


def _load_mcp_servers(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict) and isinstance(parsed.get("servers"), list):
        return [item for item in parsed["servers"] if isinstance(item, dict)]
    return []
