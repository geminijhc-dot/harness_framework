#!/usr/bin/env bash
# PreToolUse hook: 위험한 명령을 차단한다.
# Claude Code는 tool 입력을 stdin JSON으로 넘기고, exit 2일 때만 호출을 막는다(stderr가 Claude에게 전달됨).
# 패턴 기반의 최소 안전장치일 뿐 완전한 차단은 아니다.
input=$(cat)
if printf '%s' "$input" | grep -qiE 'rm[[:space:]]+-rf|git[[:space:]]+push[[:space:]]+--force|git[[:space:]]+reset[[:space:]]+--hard|DROP[[:space:]]+TABLE|Remove-Item[^"]*-Recurse'; then
  echo "BLOCKED: 위험한 명령어가 감지되었습니다." >&2
  exit 2
fi
exit 0
