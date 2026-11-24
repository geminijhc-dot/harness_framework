"""report.py 집계 테스트."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import report as rp

ROWS = [
    {"step": 0, "name": "setup", "attempt": 1, "outcome": "completed", "reported_status": "completed",
     "verify": "pass", "cost_usd": 0.5, "num_turns": 10, "elapsed_s": 60, "session_id": "a"},
    {"step": 1, "name": "api", "attempt": 1, "outcome": "retry", "reported_status": "completed",
     "verify": "ac_fail", "cost_usd": 1.0, "num_turns": 20, "elapsed_s": 100, "session_id": "b",
     "session_committed": True, "error": "하네스 AC 재검증 실패 (exit 1)\nTS2304"},
    {"step": 1, "name": "api", "attempt": 2, "outcome": "completed", "reported_status": "completed",
     "verify": "pass", "cost_usd": 0.75, "num_turns": 5, "elapsed_s": 40, "session_id": "c",
     "tests_modified": ["tests/test_api.py"]},
]


def test_summarize_per_step():
    s = rp.summarize(ROWS)
    assert s[0]["attempts"] == 1 and s[0]["false_completions"] == 0
    assert s[1]["attempts"] == 2 and s[1]["false_completions"] == 1
    assert s[1]["cost_usd"] == 1.75 and s[1]["turns"] == 25 and s[1]["outcome"] == "completed"
    assert s[1]["tests_modified"] == {"tests/test_api.py"}


def test_false_completion_requires_reported_completed():
    assert not rp.is_false_completion({"reported_status": "pending", "verify": None})
    assert rp.is_false_completion({"reported_status": "completed", "verify": "tests_deleted"})


def test_transcript_path_encoding():
    p = rp.transcript_path(Path(r"C:\Users\me\proj"), "sid")
    assert p.parent.name == "C--Users-me-proj"
    assert p.name == "sid.jsonl"


def test_render_lists_failures(tmp_path):
    (tmp_path / "metrics.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    text = rp.render(rp.load_rows(tmp_path), tmp_path)
    assert "잘못된 완료 보고: 1/3" in text
    assert "회수된 시도: 1회" in text
    assert "step 1 #1 retry" in text and "transcript:" in text


def test_render_empty():
    assert "기록이 없습니다" in rp.render([], Path("."))
