"""위험 명령 차단 hook(.claude/hooks/block-dangerous.sh) 판정 테스트.

Claude Code는 tool 입력을 stdin JSON으로 넘기고 exit 2일 때만 호출을 막는다.
OS마다 grep 구현(GNU/BSD)이 달라 CI에서 각 OS로 검증한다.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block-dangerous.sh"
BASH = shutil.which("bash") or "bash"


def run_hook(tool: str, command: str) -> int:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    return subprocess.run([BASH, str(HOOK)], input=payload, capture_output=True, text=True,
                          encoding="utf-8").returncode


@pytest.mark.parametrize("tool,command", [
    ("Bash", "rm -rf build"),
    ("Bash", "git push --force origin main"),
    ("Bash", "git reset --hard HEAD~1"),
    ("Bash", "psql -c 'drop table users'"),
    ("PowerShell", "Remove-Item -Path build -Recurse -Force"),
    ("PowerShell", 'Remove-Item -Path "my build" -Recurse -Force'),
])
def test_blocks_dangerous(tool, command):
    assert run_hook(tool, command) == 2


@pytest.mark.parametrize("tool,command", [
    ("Bash", "npm test"),
    ("Bash", "rm build.log"),
    ("Bash", "git push origin main"),
    ("PowerShell", "Remove-Item build.log"),
])
def test_allows_safe(tool, command):
    assert run_hook(tool, command) == 0
