from __future__ import annotations

import subprocess
import sys


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "claude_code_python.cli",
        "Use Bash to print 'terminalbench smoke' and then stop.",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=180)
    print(completed.stdout)
    print(completed.stderr, file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
