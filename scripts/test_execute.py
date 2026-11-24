"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two", encoding="utf-8")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content", encoding="utf-8")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc", encoding="utf-8")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.", encoding="utf-8")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text(encoding="utf-8")
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text(encoding="utf-8")
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx), encoding="utf-8")
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")

    def test_long_summary_is_clipped(self):
        limit = ex.StepExecutor.SUMMARY_MAX_CHARS
        index = {"steps": [
            {"step": 0, "name": "big", "status": "completed", "summary": "가" * (limit + 500)},
            {"step": 1, "name": "small", "status": "completed", "summary": "짧음"},
        ]}
        result = ex.StepExecutor._build_step_context(index)
        assert "가" * limit + "…" in result
        assert "가" * (limit + 1) not in result
        assert "짧음" in result and "짧음…" not in result


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_forbids_session_commits(self, executor):
        # 커밋은 하네스가 검증 통과 후에만 한다. 세션에게 커밋을 지시하지 않는다.
        result = executor._build_preamble("", "")
        assert "git commit을 하지 마라" in result
        assert "feat(mvp):" not in result

    def test_forbids_deleting_tests(self, executor):
        result = executor._build_preamble("", "")
        assert "삭제" in result and "테스트" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text(encoding="utf-8"))
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text(encoding="utf-8"))
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        # 프롬프트는 명령줄 인자가 아니라 stdin으로 넘긴다 (Windows 명령줄 길이 제한 회피)
        assert not any("PREAMBLE" in c for c in cmd)
        stdin = mock_run.call_args[1]["input"]
        assert "PREAMBLE" in stdin
        assert "UI를 구현하세요" in stdin

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text(encoding="utf-8"))
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_becomes_failed_output(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1800)):
            output = executor._invoke_claude(step, "preamble")
        assert output["exitCode"] == -1
        assert "TIMEOUT" in output["stderr"]

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index), encoding="utf-8")

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# AC 독립 재검증
# ---------------------------------------------------------------------------

STEP_WITH_AC = textwrap.dedent("""\
    # Step 2: UI

    ## 작업

    ```bash
    echo not-ac
    ```

    ## Acceptance Criteria

    ```bash
    npm run build   # 컴파일 에러 없음
    npm test
    ```

    ## 검증 절차

    1. 위 AC 커맨드를 실행한다.
""")


class TestExtractAc:
    def test_extracts_block_from_ac_section_only(self):
        script = ex.StepExecutor._extract_ac(STEP_WITH_AC)
        assert "npm run build" in script
        assert "npm test" in script
        assert "not-ac" not in script

    def test_missing_section_returns_none(self):
        assert ex.StepExecutor._extract_ac("# Step\n\n## 작업\n\n```bash\nls\n```\n") is None

    def test_section_without_code_block_returns_none(self):
        assert ex.StepExecutor._extract_ac("## Acceptance Criteria\n\n빌드가 되어야 한다.\n\n## 금지사항\n") is None


class TestRunAc:
    def test_passing_script(self, executor):
        ok, log = executor._run_ac("true\n")
        assert ok and log == ""

    def test_failing_script_reports_exit_and_output(self, executor):
        ok, log = executor._run_ac("echo boom-output\nexit 3\n")
        assert not ok
        assert "exit 3" in log
        assert "boom-output" in log

    def test_stops_at_first_failing_line(self, executor):
        ok, log = executor._run_ac("false\necho should-not-run\n")
        assert not ok
        assert "should-not-run" not in log

    def test_silent_failure_shows_failing_command(self, executor):
        ok, log = executor._run_ac("true\ngrep -q nothing-here /dev/null\n")
        assert not ok
        assert "grep -q nothing-here" in log

    def test_pipefail(self, executor):
        ok, _ = executor._run_ac("false | true\n")
        assert not ok

    def test_runs_in_project_root(self, executor, tmp_project):
        (tmp_project / "marker.txt").write_text("x", encoding="utf-8")
        ok, _ = executor._run_ac("test -f marker.txt\n")
        assert ok


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout.strip()


@pytest.fixture
def git_executor(executor, tmp_project, phase_dir):
    """tmp_project를 독립 git 저장소로 만든 executor. 상위 디렉토리의 저장소가 잡히지 않게 한다."""
    (phase_dir / "step2.md").write_text(STEP_WITH_AC, encoding="utf-8")
    (tmp_project / "tests").mkdir()
    (tmp_project / "tests" / "test_core.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (tmp_project / "tests" / "test_other.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    (tmp_project / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_project, "init", "-q")
    _git(tmp_project, "config", "user.email", "t@example.com")
    _git(tmp_project, "config", "user.name", "t")
    _git(tmp_project, "config", "commit.gpgsign", "false")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-q", "-m", "base")
    return executor


SESSION_JSON = json.dumps({"type": "result", "session_id": "sess-1", "num_turns": 7,
                           "total_cost_usd": 0.42, "duration_ms": 1234, "is_error": False})


def _fake_session(executor, *, status="completed", action=None):
    """세션 흉내: action(작업)을 수행하고 index.json에 status를 기록한 뒤 claude 출력 dict를 돌려준다."""
    def fake_invoke(step, preamble):
        if action:
            action()
        idx = executor._read_json(executor._index_file)
        for s in idx["steps"]:
            if s["step"] == 2:
                s["status"] = status
                if status == "completed":
                    s["summary"] = "UI 완료"
                if status == "blocked":
                    s["blocked_reason"] = "API 키 필요"
        executor._write_json(executor._index_file, idx)
        return {"step": 2, "name": "ui", "exitCode": 0, "stdout": SESSION_JSON, "stderr": ""}
    return fake_invoke


def _metrics(phase_dir):
    p = phase_dir / "metrics.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


class TestSessionStats:
    def test_parses_claude_json(self):
        stats = ex.StepExecutor._session_stats({"exitCode": 0, "stdout": SESSION_JSON})
        assert stats["session_id"] == "sess-1"
        assert stats["num_turns"] == 7
        assert stats["cost_usd"] == 0.42
        assert stats["duration_ms"] == 1234

    def test_garbage_stdout_gives_nulls(self):
        stats = ex.StepExecutor._session_stats({"exitCode": 1, "stdout": "not json"})
        assert stats["exit_code"] == 1
        assert stats["session_id"] is None


class TestSessionCommitsAndTests:
    def test_uncommit_session_commits_keeps_changes(self, git_executor, tmp_project):
        base = git_executor._head()
        (tmp_project / "app.py").write_text("x = 2\n", encoding="utf-8")
        _git(tmp_project, "commit", "-qam", "session commit")
        assert git_executor._uncommit_session_commits(base) is True
        assert git_executor._head() == base
        assert (tmp_project / "app.py").read_text(encoding="utf-8") == "x = 2\n"

    def test_no_session_commit_is_noop(self, git_executor):
        base = git_executor._head()
        assert git_executor._uncommit_session_commits(base) is False

    def test_changed_test_files(self, git_executor, tmp_project):
        base = git_executor._head()
        (tmp_project / "tests" / "test_core.py").unlink()
        (tmp_project / "tests" / "test_other.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
        (tmp_project / "app.py").unlink()  # 테스트가 아닌 파일 삭제는 무시
        deleted, modified = git_executor._changed_test_files(base)
        assert deleted == ["tests/test_core.py"]
        assert modified == ["tests/test_other.py"]

    def test_git_mv_of_test_is_not_deletion(self, git_executor, tmp_project):
        base = git_executor._head()
        (tmp_project / "tests" / "unit").mkdir()
        _git(tmp_project, "mv", "tests/test_core.py", "tests/unit/test_core.py")
        deleted, _ = git_executor._changed_test_files(base)
        assert deleted == []

    @pytest.mark.parametrize("path,is_test", [
        ("tests/test_api.py", True), ("pkg/api_test.py", True), ("cmd/x_test.go", True),
        ("src/a.test.ts", True), ("src/a.spec.tsx", True), ("src/__tests__/a.js", True),
        ("src/test/java/com/x/UserServiceTest.java", True), ("app/src/test/UserRepoTests.kt", True),
        ("Tests/AppTests.swift", True), ("tests/Unit/UserTest.php", True), ("Api.Tests/UserTests.cs", True),
        ("spec/models/user_spec.rb", True), ("test/user_test.rb", True), ("test/widget_test.dart", True),
        ("test/app_test.exs", True), ("src/test/UserSpec.groovy", True), ("tests/integration.rs", True),
        ("src/testing_utils.py", False), ("src/latest.ts", False), ("docs/test-plan.md", False),
        ("src/main/java/com/x/Contest.java", False), ("src/Testimony.kt", False),
        ("src/lib.rs", False), ("tests/common/mod.rs", False), ("src/Inspec.kt", False),
    ])
    def test_test_file_pattern(self, path, is_test):
        assert bool(ex.StepExecutor.TEST_FILE_RE.search(path)) is is_test


class TestExecuteSingleStepAc:
    STEP = {"step": 2, "name": "ui", "status": "pending"}

    def test_completed_and_ac_pass_commits_once_after_verification(self, git_executor, tmp_project, phase_dir):
        def work():
            (tmp_project / "ui.py").write_text("ui = True\n", encoding="utf-8")
            _git(tmp_project, "add", "-A")
            _git(tmp_project, "commit", "-qm", "session made its own commit")

        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor, action=work)), \
             patch.object(git_executor, "_run_ac", return_value=(True, "")) as run_ac:
            assert git_executor._execute_single_step(self.STEP, "") is True
        run_ac.assert_called_once()

        log = _git(tmp_project, "log", "--format=%s")
        assert "session made its own commit" not in log  # 세션 커밋은 회수됨
        assert "feat(mvp): step 2 — ui" in log
        # metrics.jsonl은 feat 커밋이 아니라 chore 커밋에 들어간다
        feat_files = _git(tmp_project, "show", "--name-only", "--format=", "HEAD~1")
        assert "ui.py" in feat_files and "metrics.jsonl" not in feat_files
        chore_files = _git(tmp_project, "show", "--name-only", "--format=", "HEAD")
        assert "metrics.jsonl" in chore_files

        step = git_executor._read_json(git_executor._index_file)["steps"][2]
        assert step["status"] == "completed" and "completed_at" in step
        [row] = _metrics(phase_dir)
        assert row["outcome"] == "completed" and row["verify"] == "pass"
        assert row["session_committed"] is True
        assert row["cost_usd"] == 0.42 and row["num_turns"] == 7 and row["session_id"] == "sess-1"

    def test_completed_but_ac_fail_is_retried_then_error_without_commit(self, git_executor, tmp_project, phase_dir):
        head_before = git_executor._head()
        ac_fail = (False, "하네스 AC 재검증 실패 (exit 1). 실제 출력:\nTS2304 error")

        def work():
            (tmp_project / "ui.py").write_text("broken\n", encoding="utf-8")
            _git(tmp_project, "add", "ui.py")  # 세션이 stage까지 해둔 경우

        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor, action=work)) as invoke, \
             patch.object(git_executor, "_run_ac", return_value=ac_fail), \
             patch.object(git_executor, "_update_top_index") as top:
            with pytest.raises(SystemExit) as exc_info:
                git_executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        assert invoke.call_count == ex.StepExecutor.MAX_RETRIES
        assert "TS2304 error" in invoke.call_args_list[1][0][1]  # 실제 AC 출력이 재시도 프롬프트로
        # 실패 기록(index·metrics)만 커밋되고, 검증 실패 코드는 커밋되지 않는다
        assert _git(tmp_project, "rev-parse", "HEAD~1") == head_before
        assert _git(tmp_project, "log", "-1", "--format=%s") == "chore(mvp): step 2 failed"
        committed = set(_git(tmp_project, "show", "--name-only", "--format=", "HEAD").splitlines())
        assert committed == {"phases/0-mvp/index.json", "phases/0-mvp/metrics.jsonl"}
        assert (tmp_project / "ui.py").exists()     # 변경은 working tree에 남음
        step = git_executor._read_json(git_executor._index_file)["steps"][2]
        assert step["status"] == "error"
        assert "TS2304 error" in step["error_message"]
        assert "summary" not in step
        top.assert_called_once_with("error")
        rows = _metrics(phase_dir)
        assert [r["outcome"] for r in rows] == ["retry", "retry", "error"]
        assert all(r["reported_status"] == "completed" and r["verify"] == "ac_fail" for r in rows)

    def test_deleted_test_file_fails_without_running_ac(self, git_executor, tmp_project, phase_dir):
        def work():
            (tmp_project / "tests" / "test_core.py").unlink(missing_ok=True)

        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor, action=work)) as invoke, \
             patch.object(git_executor, "_run_ac", return_value=(True, "")) as run_ac, \
             patch.object(git_executor, "_update_top_index"):
            with pytest.raises(SystemExit):
                git_executor._execute_single_step(self.STEP, "")
        run_ac.assert_not_called()
        assert "tests/test_core.py" in invoke.call_args_list[1][0][1]  # 복원 대상이 재시도 프롬프트로
        assert _metrics(phase_dir)[0]["verify"] == "tests_deleted"

    def test_blocked_is_recorded(self, git_executor, phase_dir):
        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor, status="blocked")), \
             patch.object(git_executor, "_update_top_index"):
            with pytest.raises(SystemExit) as exc_info:
                git_executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 2
        [row] = _metrics(phase_dir)
        assert row["outcome"] == "blocked"

    def test_status_is_pending_while_ac_runs_and_interruption_is_recorded(self, git_executor, phase_dir):
        def interrupted_ac(script):
            raise KeyboardInterrupt

        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor)), \
             patch.object(git_executor, "_run_ac", side_effect=interrupted_ac):
            with pytest.raises(KeyboardInterrupt):
                git_executor._execute_single_step(self.STEP, "")
        step = git_executor._read_json(git_executor._index_file)["steps"][2]
        assert step["status"] == "pending"
        [row] = _metrics(phase_dir)
        assert row["outcome"] == "interrupted"

    def test_prompt_size_is_recorded_and_budget_warned(self, git_executor, phase_dir, capsys):
        guardrails = "규칙" * 100
        with patch.object(ex.StepExecutor, "PROMPT_BUDGET_CHARS", 50), \
             patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor)), \
             patch.object(git_executor, "_run_ac", return_value=(True, "")):
            git_executor._execute_single_step(self.STEP, guardrails)
        [row] = _metrics(phase_dir)
        assert row["guardrails_chars"] == len(guardrails)
        assert row["prompt_chars"] > len(guardrails) + len(STEP_WITH_AC) - 1
        assert row["context_chars"] > 0  # step 0, 1 요약
        assert row["over_budget"] is True
        assert "기준 50자 초과" in capsys.readouterr().out

    def test_prompt_within_budget_is_not_flagged(self, git_executor, phase_dir):
        with patch.object(git_executor, "_invoke_claude", side_effect=_fake_session(git_executor)), \
             patch.object(git_executor, "_run_ac", return_value=(True, "")):
            git_executor._execute_single_step(self.STEP, "")
        assert _metrics(phase_dir)[0]["over_budget"] is False

    def test_missing_ac_block_exits_before_invoking(self, executor, phase_dir):
        (phase_dir / "step2.md").write_text("# Step 2\n\n## 작업\n\nUI\n", encoding="utf-8")
        with patch.object(executor, "_invoke_claude") as invoke:
            with pytest.raises(SystemExit) as exc_info:
                executor._execute_single_step(self.STEP, "")
        assert exc_info.value.code == 1
        invoke.assert_not_called()
