from __future__ import annotations

import re
from pathlib import Path


class PermissionError(RuntimeError):
    pass


class PermissionManager:
    _DANGEROUS_PATTERNS: tuple[str, ...] = (
        r"\brm\s+-rf\s+/\b",
        r"\b(?:shutdown|reboot|mkfs|diskpart)\b",
        r"\bformat\s+[a-z]:",
        r"\bremove-item\b.*-recurse\b.*-force\b",
        r"\b(?:invoke-expression|iex)\b",
        r"-encodedcommand\b|\s-enc\s",
        r"frombase64string\s*\(",
        r"\brd\s+/s\b",
        r"\bdel\s+/s\b",
    )

    def __init__(self, workspace: Path, mode: str = "ask"):
        self.workspace = workspace.resolve()
        self.mode = mode

    def resolve_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()

    def require_workspace_path(self, path: str | Path, action: str) -> Path:
        candidate = self.resolve_path(path)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError(f"{action} denied outside workspace: {candidate}") from exc
        return candidate

    def can_run_command(self, command: str) -> None:
        normalized = " ".join(command.strip().lower().split())
        for pattern in self._DANGEROUS_PATTERNS:
            if re.search(pattern, normalized):
                raise PermissionError(f"dangerous command denied by policy: {command[:120]}")
