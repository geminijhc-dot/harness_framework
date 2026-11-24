#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    AC_TIMEOUT = 900
    AC_LOG_LIMIT = 3000
    # 이전 step 요약 한 건의 최대 길이. 세션이 길게 써도 다음 step 프롬프트가 무한히 커지지 않게 자른다.
    SUMMARY_MAX_CHARS = 200
    # step 프롬프트 크기 경고 기준. 넘으면 규칙 문서를 지도(목차) 방식으로 바꾸거나 요약을 줄일 시점이다.
    PROMPT_BUDGET_CHARS = 30000
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        self._print_header()
        self._check_blockers()
        self._checkout_branch()
        guardrails = self._load_guardrails()
        self._ensure_created_at()
        self._execute_all_steps(guardrails)
        self._finalize()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def _checkout_branch(self):
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"
        metrics_rel = f"phases/{self._phase_dir_name}/metrics.jsonl"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)
        self._run_git("reset", "HEAD", "--", metrics_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    def _commit_failure_metadata(self, step_num: int):
        """실패 기록(index·metrics)만 커밋한다. 코드 변경은 사람이 확인하도록 working tree에 남긴다."""
        paths = [f"phases/{self._phase_dir_name}/index.json",
                 f"phases/{self._phase_dir_name}/metrics.jsonl",
                 "phases/index.json"]
        paths = [p for p in paths if (Path(self._root) / p).exists()]
        self._run_git("add", "--", *paths)
        # 경로를 지정한 commit은 해당 경로만 커밋하므로, 세션이 stage해 둔 코드 변경은 섞이지 않는다.
        r = self._run_git("commit", "-m", f"chore({self._phase_name}): step {step_num} failed", "--", *paths)
        if r.returncode != 0 and "nothing" not in (r.stdout + r.stderr):
            print(f"  WARN: 실패 기록 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text(encoding='utf-8')}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text(encoding='utf-8')}")
        return "\n\n---\n\n".join(sections) if sections else ""

    @staticmethod
    def _build_step_context(index: dict) -> str:
        limit = StepExecutor.SUMMARY_MAX_CHARS
        lines = [
            f"- Step {s['step']} ({s['name']}): "
            + (s["summary"] if len(s["summary"]) <= limit else s["summary"][:limit] + "…")
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리거나, 삭제하거나, 통과시키려고 약화시키지 마라. "
            f"기존 테스트 파일이 삭제되면 하네스가 실패로 처리한다.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라. "
            f"세션 종료 후 하네스가 step 파일의 AC 커맨드를 독립적으로 재실행하며, "
            f"실패하면 completed로 기록했더라도 실패로 처리된다.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄({self.SUMMARY_MAX_CHARS}자 이내)로 요약\n"
            f"   - {self.MAX_RETRIES}회 수정 시도 후에도 실패 → \"error\" + \"error_message\" 기록\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. git commit을 하지 마라. 하네스가 검증 통과 후 직접 커밋하며, "
            f"세션이 만든 커밋은 되돌려진다(변경 내용은 유지).\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text(encoding="utf-8")
        try:
            # 프롬프트는 stdin으로 넘긴다. 인자로 넘기면 Windows 명령줄 길이 제한(약 32K자)에 걸려 실행이 실패한다.
            result = subprocess.run(
                ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"],
                input=prompt, cwd=self._root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
        except subprocess.TimeoutExpired:
            result = types.SimpleNamespace(returncode=-1, stdout="", stderr="TIMEOUT: claude 세션이 1800초 내에 끝나지 않음")

        if result.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    # --- AC 독립 재검증 ---

    @staticmethod
    def _extract_ac(step_md: str) -> Optional[str]:
        """step 파일의 '## Acceptance Criteria' 섹션에서 첫 코드 블록(bash)을 꺼낸다."""
        section = re.search(r"^## Acceptance Criteria[ \t]*\n(.*?)(?=^## |\Z)", step_md, re.M | re.S)
        if not section:
            return None
        block = re.search(r"```(?:bash|sh)?[ \t]*\n(.*?)```", section.group(1), re.S)
        if not block or not block.group(1).strip():
            return None
        return block.group(1)

    def _run_ac(self, script: str) -> tuple[bool, str]:
        """AC 스크립트를 세션과 무관하게 bash로 실행한다. (통과 여부, 실패 로그)."""
        # Windows에서 "bash"를 그대로 넘기면 System32의 WSL bash가 먼저 잡히므로 PATH 기준으로 찾는다.
        bash = shutil.which("bash") or "bash"
        try:
            r = subprocess.run(
                # -x: 출력이 없는 명령(grep -q 등)이 실패해도 어느 줄에서 실패했는지 로그에 남도록 한다.
                [bash, "-e", "-x", "-o", "pipefail", "-c", script],
                cwd=self._root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=self.AC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, f"AC 재검증 시간 초과 ({self.AC_TIMEOUT}s)"
        if r.returncode == 0:
            return True, ""
        log = (r.stdout or "")[-self.AC_LOG_LIMIT:]
        return False, f"하네스 AC 재검증 실패 (exit {r.returncode}). 실제 출력:\n{log}"

    # --- 세션 커밋 회수 & 테스트 보호 ---

    # 테스트 파일로 간주하는 경로. 오탐 시 재시도 3회가 낭비되므로 좁게 유지한다.
    TEST_FILE_RE = re.compile(
        r"(^|/)(test_[^/]+\.py|[^/]+_test\.(py|go)|[^/]+\.(test|spec)\.[cm]?[jt]sx?)$|(^|/)__tests__/"
    )

    def _head(self) -> Optional[str]:
        r = self._run_git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None

    def _uncommit_session_commits(self, base: Optional[str]) -> bool:
        """세션이 직접 만든 커밋을 soft reset으로 회수한다(변경 내용은 working tree에 유지)."""
        head = self._head()
        if not base or not head or head == base:
            return False
        if self._run_git("merge-base", "--is-ancestor", base, head).returncode != 0:
            print(f"  WARN: HEAD가 step 시작 시점({base[:7]})의 후손이 아니어서 세션 커밋을 되돌리지 않음")
            return False
        self._run_git("reset", "--soft", base)
        print(f"  ↺ 세션이 만든 커밋을 회수함 ({base[:7]}..{head[:7]})")
        return True

    def _changed_test_files(self, base: Optional[str]) -> tuple[list, list]:
        """step 시작 시점 대비 삭제/수정된 기존 테스트 파일 목록 (deleted, modified)."""
        if not base:
            return [], []
        # git mv로 옮긴 테스트는 R 행(3필드)으로 나와 삭제로 치지 않는다.
        r = self._run_git("diff", "--name-status", base)
        deleted, modified = [], []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or not self.TEST_FILE_RE.search(parts[1]):
                continue
            if parts[0] == "D":
                deleted.append(parts[1])
            elif parts[0] == "M":
                modified.append(parts[1])
        return deleted, modified

    # --- 측정 ---

    @staticmethod
    def _session_stats(output: dict) -> dict:
        """claude -p --output-format json 결과에서 측정 필드를 꺼낸다."""
        try:
            d = json.loads(output.get("stdout") or "")
        except (json.JSONDecodeError, TypeError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        return {
            "exit_code": output.get("exitCode"),
            "session_id": d.get("session_id"),
            "num_turns": d.get("num_turns"),
            "cost_usd": d.get("total_cost_usd"),
            "duration_ms": d.get("duration_ms"),
            "is_error": d.get("is_error"),
        }

    def _append_metrics(self, row: dict):
        with open(self._phase_dir / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None

        step_file = self._phase_dir / f"step{step_num}.md"
        step_md = step_file.read_text(encoding="utf-8") if step_file.exists() else ""
        ac_script = self._extract_ac(step_md) if step_file.exists() else None
        if step_file.exists() and ac_script is None:
            print(f"  ERROR: {step_file} 에 '## Acceptance Criteria' bash 블록이 없어 검증할 수 없습니다.")
            sys.exit(1)

        # 세션 커밋 회수와 테스트 삭제 검사의 기준점. 실패 시도는 커밋하지 않으므로 재시도 간에 유지된다.
        base = self._head()

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            # 모든 시도를 metrics.jsonl에 한 줄씩 남긴다. 정상 분기에서 기록하지 못하면(중단·예외) finally에서 기록.
            row = {"ts": self._stamp(), "step": step_num, "name": step_name, "attempt": attempt,
                   "agent": "claude", "outcome": "interrupted"}
            prompt_chars = len(preamble) + len(step_md)
            row.update(prompt_chars=prompt_chars, guardrails_chars=len(guardrails),
                       context_chars=len(step_context),
                       over_budget=prompt_chars > self.PROMPT_BUDGET_CHARS)
            if row["over_budget"] and attempt == 1:
                print(f"  ⚠ 프롬프트 {prompt_chars:,}자 — 기준 {self.PROMPT_BUDGET_CHARS:,}자 초과 "
                      f"(규칙 문서 {len(guardrails):,} / 이전 step 요약 {len(step_context):,} / step 파일 {len(step_md):,}). "
                      f"문서를 지도(목차) 방식으로 바꾸거나 요약을 줄일 시점입니다.")
            recorded = False
            try:
                t0 = time.monotonic()
                with progress_indicator(tag):
                    output = self._invoke_claude(step, preamble)
                elapsed = int(time.monotonic() - t0)
                row.update(self._session_stats(output), elapsed_s=elapsed)
                row["session_committed"] = self._uncommit_session_commits(base)

                index = self._read_json(self._index_file)
                status = next((s.get("status", "pending") for s in index["steps"] if s["step"] == step_num), "pending")
                row["reported_status"] = status
                ts = self._stamp()

                fail_log = ""
                if status == "completed":
                    # 재검증 도중 중단돼도 미검증 step이 완료로 남지 않도록 통과 전까지는 pending으로 되돌린다.
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s["status"] = "pending"
                    self._write_json(self._index_file, index)

                    deleted, modified = self._changed_test_files(base)
                    row["tests_deleted"], row["tests_modified"] = deleted, modified
                    if deleted:
                        row["verify"] = "tests_deleted"
                        fail_log = "기존 테스트 파일이 삭제됨 — 반드시 복원하라: " + ", ".join(deleted)
                    else:
                        ac_ok, fail_log = self._run_ac(ac_script)
                        row["verify"] = "pass" if ac_ok else "ac_fail"

                    if row["verify"] == "pass":
                        for s in index["steps"]:
                            if s["step"] == step_num:
                                s["status"] = "completed"
                                s["completed_at"] = ts
                        self._write_json(self._index_file, index)
                        row["outcome"] = "completed"
                        self._append_metrics(row)
                        recorded = True
                        self._commit_step(step_num, step_name)
                        print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                        return True
                    print(f"  ✗ Step {step_num}: 세션은 completed로 보고했지만 하네스 검증 실패 ({row['verify']})")

                if status == "blocked":
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s["blocked_at"] = ts
                    self._write_json(self._index_file, index)
                    reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
                    row["outcome"] = "blocked"
                    self._append_metrics(row)
                    recorded = True
                    print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                    print(f"    Reason: {reason}")
                    self._update_top_index("blocked")
                    sys.exit(2)

                err_msg = fail_log or next(
                    (s.get("error_message", "Step did not update status") for s in index["steps"] if s["step"] == step_num),
                    "Step did not update status",
                )
                row["error"] = err_msg[:500]

                if attempt < self.MAX_RETRIES:
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s["status"] = "pending"
                            s.pop("error_message", None)
                            s.pop("summary", None)
                    self._write_json(self._index_file, index)
                    row["outcome"] = "retry"
                    self._append_metrics(row)
                    recorded = True
                    prev_error = err_msg
                    print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
                else:
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s["status"] = "error"
                            s.pop("summary", None)
                            s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                            s["failed_at"] = ts
                    self._write_json(self._index_file, index)
                    row["outcome"] = "error"
                    self._append_metrics(row)
                    recorded = True
                    # 검증을 통과하지 못한 코드는 커밋하지 않는다. 변경 내용은 working tree에 남겨 사람이 확인한다.
                    print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                    print(f"    Error: {err_msg}")
                    print(f"    변경 내용은 커밋되지 않고 working tree에 남아 있습니다.")
                    self._update_top_index("error")
                    self._commit_failure_metadata(step_num)
                    sys.exit(1)
            finally:
                if not recorded:
                    self._append_metrics(row)

        return False  # unreachable

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        if self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    # 출력이 파일/파이프로 리다이렉트되면 Windows에서는 cp949가 기본이라 ✓·↻ 같은 기호에서 깨진다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push).run()


if __name__ == "__main__":
    main()
