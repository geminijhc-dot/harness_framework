#!/usr/bin/env python3
"""
Harness 실행 측정 리포트 — phases/<phase-dir>/metrics.jsonl 을 step별로 집계한다.

Usage:
    python scripts/report.py <phase-dir>
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(phase_dir: Path) -> list:
    p = phase_dir / "metrics.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_false_completion(row: dict) -> bool:
    """세션은 completed라고 보고했지만 하네스 검증에서 떨어진 시도."""
    return row.get("reported_status") == "completed" and row.get("verify") not in (None, "pass")


def summarize(rows: list) -> dict:
    """step 번호 → 집계 dict."""
    steps = {}
    for r in rows:
        s = steps.setdefault(r["step"], {
            "name": r.get("name"), "attempts": 0, "false_completions": 0, "cost_usd": 0.0,
            "turns": 0, "elapsed_s": 0, "tests_modified": set(), "outcome": None,
        })
        s["attempts"] += 1
        s["false_completions"] += is_false_completion(r)
        s["cost_usd"] += r.get("cost_usd") or 0
        s["turns"] += r.get("num_turns") or 0
        s["elapsed_s"] += r.get("elapsed_s") or 0
        s["tests_modified"].update(r.get("tests_modified") or [])
        s["outcome"] = r.get("outcome")
    return steps


def transcript_path(root: Path, session_id: str) -> Path:
    """Claude Code가 세션 기록을 저장하는 위치 (~/.claude/projects/<인코딩된 경로>/<session>.jsonl)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root)) / f"{session_id}.jsonl"


def render(rows: list, root: Path) -> str:
    if not rows:
        return "metrics.jsonl 에 기록이 없습니다."
    steps = summarize(rows)
    out = []
    total_cost = sum(s["cost_usd"] for s in steps.values())
    total_time = sum(s["elapsed_s"] for s in steps.values())
    false_total = sum(s["false_completions"] for s in steps.values())
    reported = sum(1 for r in rows if r.get("reported_status") == "completed")
    out.append(f"시도 {len(rows)}회 · step {len(steps)}개 · 비용 ${total_cost:.2f} · 시간 {total_time}s")
    out.append(f"잘못된 완료 보고: {false_total}/{reported} "
               f"(세션이 completed라 했지만 하네스 검증 실패)")
    out.append(f"세션이 직접 커밋해 회수된 시도: {sum(1 for r in rows if r.get('session_committed'))}회")
    out.append("")
    out.append(f"{'step':<5}{'name':<20}{'시도':>4}{'잘못된완료':>8}{'비용$':>8}{'턴':>5}{'시간s':>7}  결과 / 수정된 테스트")
    for num in sorted(steps):
        s = steps[num]
        tests = f" / tests: {', '.join(sorted(s['tests_modified']))}" if s["tests_modified"] else ""
        out.append(f"{num:<5}{(s['name'] or '')[:19]:<20}{s['attempts']:>4}{s['false_completions']:>8}"
                   f"{s['cost_usd']:>8.2f}{s['turns']:>5}{s['elapsed_s']:>7}  {s['outcome']}{tests}")

    failed = [r for r in rows if r.get("outcome") != "completed"]
    if failed:
        out.append("")
        out.append("실패/중단 시도 — 아래 세션 기록을 직접 읽고 원인을 확인하라:")
        for r in failed:
            err = (r.get("error") or "").strip().splitlines()
            out.append(f"- step {r['step']} #{r['attempt']} {r.get('outcome')}"
                       f" (verify={r.get('verify')}): {err[0][:120] if err else ''}")
            if r.get("session_id"):
                p = transcript_path(root, r["session_id"])
                out.append(f"  transcript: {p}{'' if p.exists() else ' (없음)'}")
    return "\n".join(out)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Harness metrics report")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    args = parser.parse_args()
    phase_dir = ROOT / "phases" / args.phase_dir
    if not phase_dir.is_dir():
        print(f"ERROR: {phase_dir} not found")
        sys.exit(1)
    print(render(load_rows(phase_dir), ROOT))


if __name__ == "__main__":
    main()
