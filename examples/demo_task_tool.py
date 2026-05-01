from __future__ import annotations

import asyncio

from claude_code_python.config import Config
from claude_code_python.runner import AgentRunner
from claude_code_python.tools.default import build_default_registry


async def main() -> None:
    runner = AgentRunner(config=Config.from_env(), registry=build_default_registry())
    result = await runner.run(
        """
Use the Agent tool to launch three independent sub-agents in parallel:
- researcher: inspect repository files
- tester: run a harmless Bash command
- reviewer: list implementation risks
Return a combined summary.
"""
    )
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
