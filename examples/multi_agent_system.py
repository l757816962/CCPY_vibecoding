from __future__ import annotations

import asyncio

from claude_code_python.config import Config
from claude_code_python.runner import AgentRunner
from claude_code_python.tools.default import build_default_registry


async def main() -> None:
    runner = AgentRunner(config=Config.from_env(), registry=build_default_registry())
    result = await runner.run(
        """
You are a coordinator. Start coder, tester, and reviewer Agent workers.
Use TaskOutput to collect their outputs and produce a final engineering report.
"""
    )
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
