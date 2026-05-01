from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


PROJECT_MEMORY_FILES = ("CLAUDE.md", "AGENTS.md", ".ccpy/memory.md")
MEMORY_DIR = ".ccpy/memories"


@dataclass(slots=True)
class MemoryEntry:
    source: str
    content: str


@dataclass(slots=True)
class MemoryHeader:
    source: str
    description: str
    mtime: float


def load_project_memory(workspace: Path, max_chars: int = 20_000) -> str:
    """Load project-level memory files into a bounded system prompt section."""
    entries: list[MemoryEntry] = []
    for relative in PROJECT_MEMORY_FILES:
        path = workspace / relative
        if path.is_file():
            entries.append(MemoryEntry(relative, _read_limited(path, max_chars)))

    memory_dir = workspace / MEMORY_DIR
    if memory_dir.is_dir():
        index_path = memory_dir / "MEMORY.md"
        if index_path.is_file():
            entries.append(MemoryEntry(str(index_path.relative_to(workspace)), _read_limited(index_path, max_chars)))
        manifest = format_memory_manifest(scan_memory_files(memory_dir, workspace=workspace))
        if manifest:
            entries.append(MemoryEntry(f"{MEMORY_DIR}/manifest", manifest))
        for path in sorted(memory_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:20]:
            if path.name.upper() == "MEMORY.md":
                continue
            relative = str(path.relative_to(workspace))
            entries.append(MemoryEntry(relative, _read_limited(path, max_chars)))

    if not entries:
        return ""

    remaining = max_chars
    sections: list[str] = []
    for entry in entries:
        if remaining <= 0:
            break
        content = entry.content[:remaining]
        remaining -= len(content)
        sections.append(f"## {entry.source}\n{content.strip()}")

    return "# Project Memory\n" + "\n\n".join(sections)


def append_project_memory(system_prompt: str, workspace: Path, max_chars: int = 20_000) -> str:
    memory = load_project_memory(workspace, max_chars=max_chars)
    if not memory:
        return system_prompt
    return f"{system_prompt}\n\n{memory}"


def _read_limited(path: Path, max_chars: int) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def scan_memory_files(memory_dir: Path, workspace: Path | None = None, limit: int = 200) -> list[MemoryHeader]:
    headers: list[MemoryHeader] = []
    for path in sorted(memory_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        if path.name.upper() == "MEMORY.MD":
            continue
        content = _read_limited(path, 4_000)
        frontmatter = _parse_frontmatter(content)
        source = str(path.relative_to(workspace or memory_dir))
        headers.append(
            MemoryHeader(
                source=source,
                description=frontmatter.get("description") or _first_heading_or_line(content),
                mtime=path.stat().st_mtime,
            )
        )
    return headers


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    if not headers:
        return ""
    lines = ["Available project memory files:"]
    for header in headers:
        description = f": {header.description}" if header.description else ""
        lines.append(f"- {header.source}{description}")
    return "\n".join(lines)


def _parse_frontmatter(content: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    output: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            output[key.strip()] = value.strip().strip('"')
    return output


def _first_heading_or_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("# ").strip()[:160]
    return ""
