# Harness

Claude Code로 기능을 **step 단위로 나눠 자동 구현**하는 하네스입니다. 사람이 설계(문서와 step 계획)를 승인하면, step마다 새 `claude -p` 세션이 구현하고 **하네스가 직접 검증한 뒤에만** 커밋합니다.

원본: [jha0313/harness_framework](https://github.com/jha0313/harness_framework)

## 하네스가 보장하는 것

| 항목 | 동작 |
|---|---|
| 독립 검증 | 세션이 `completed`라고 보고해도 하네스가 step의 AC 명령을 직접 다시 실행해 통과해야만 완료 |
| 커밋 제어 | 검증을 통과한 코드만 하네스가 커밋. 세션이 직접 만든 커밋은 회수(변경은 유지). 실패한 코드는 커밋하지 않고 작업 폴더에 남김 |
| 테스트 보호 | 기존 테스트 파일이 삭제되면 실패 처리 |
| 재시도 | 실패 시 최대 3회. 실제 AC 출력(어느 명령이 실패했는지 포함)을 다음 시도에 전달 |
| 측정 | 모든 시도를 `phases/<task>/metrics.jsonl`에 기록 (검증 결과, 비용, 턴 수, 시간, 프롬프트 크기, session_id) |
| 위험 명령 차단 | `rm -rf`, 강제 push, `git reset --hard`, `DROP TABLE`, PowerShell 재귀 삭제를 hook으로 차단 |

## 요구사항

- [Claude Code](https://code.claude.com) CLI (`claude`)
- git
- Python 3.9 이상 (macOS 기본 `python3` 사용 가능)
- bash (macOS·Linux 기본 제공. Windows는 [Git for Windows](https://gitforwindows.org)에 포함)
- Windows에서 Python이 없다면 [uv](https://docs.astral.sh/uv/)로 실행할 수 있습니다

## 새 프로젝트에 적용하기

### 1. 파일 복사

프로젝트 루트에 아래를 복사합니다.

```
.claude/commands/harness.md    # /harness — 설계·step 생성·실행·회고 워크플로우
.claude/commands/review.md     # /review — 변경 사항 체크리스트 리뷰
.claude/hooks/block-dangerous.sh
.claude/settings.json          # 이미 있다면 hooks 항목만 합칩니다
scripts/execute.py             # step 실행기
scripts/report.py              # 측정 리포트
CLAUDE.md                      # 템플릿 — 프로젝트에 맞게 채움
docs/                          # 템플릿 — 프로젝트에 맞게 채움
```

`.gitignore`에는 아래 줄을 추가합니다.

```
phases/**/step*-output.json
```

`scripts/test_*.py`와 `.github/workflows/harness-tests.yml`은 하네스 자체를 검증하는 파일이라 복사하지 않아도 됩니다.

### 2. 프로젝트에 맞게 바꿀 것

하네스 코드(`scripts/`, `.claude/hooks/`)는 **고치지 않습니다.** 그래야 하네스가 개선됐을 때 파일을 덮어쓰는 것만으로 업데이트할 수 있습니다. 프로젝트별 내용은 모두 아래 파일에 둡니다.

| 무엇 | 어디에 |
|---|---|
| 기술 스택, 절대 규칙, 명령어 | `CLAUDE.md` (짧게 유지 — 매 step 프롬프트에 들어갑니다) |
| 기획, 아키텍처, 기술 결정, UI 규칙 | `docs/PRD.md`, `ARCHITECTURE.md`, `ADR.md`, `UI_GUIDE.md` |
| 빌드·테스트 명령 | 각 step 파일의 `## Acceptance Criteria` bash 블록 (`/harness`가 step 설계 시 작성) |
| 대화형 세션 종료 시 자동 검사 (선택) | `.claude/settings.json`의 `Stop` hook — 아래 예시 |

Stop hook 예시 (스택에 맞는 것 하나만 `hooks` 안에 추가):

```json
"Stop": [{ "matcher": "", "hooks": [{ "type": "command", "command": "npm run lint && npm test" }] }]
```

```json
"Stop": [{ "matcher": "", "hooks": [{ "type": "command", "command": "ruff check . && pytest -q" }] }]
```

step 실행 중의 검증은 Stop hook이 아니라 AC 재검증이 담당하므로, Stop hook은 없어도 됩니다.

### 3. 시작 전에 커밋

`execute.py`는 `feat-<task>` 브랜치를 만들고 `git add -A`로 커밋합니다. 커밋하지 않은 변경이 있으면 첫 step 커밋에 섞여 들어가므로, 하네스 파일과 문서를 먼저 커밋해 두세요.

## 사용 흐름

1. **설계** — Claude Code에서 `/harness`를 실행합니다. 문서를 읽고 논의한 뒤 step 계획 초안을 만들고, 승인하면 `phases/<task>/` 아래에 `index.json`과 `step{N}.md`를 생성합니다.
2. **실행**

   ```bash
   python3 scripts/execute.py <task>          # macOS / Linux
   python3 scripts/execute.py <task> --push   # 완료 후 push
   uv run --no-project python scripts/execute.py <task>   # Windows (Python 미설치 시)
   ```

3. **회고**

   ```bash
   python3 scripts/report.py <task>
   ```

   step별 시도 수, 잘못된 완료 보고(세션은 완료라 했지만 검증 실패), 비용·턴·시간·프롬프트 크기와, 실패한 시도의 세션 기록(transcript) 경로를 보여줍니다. 숫자만 보지 말고 실패한 세션 기록을 직접 읽어 원인(step 범위, AC 부실, 문서 부족, 환경 문제)을 찾고, 한 번에 한 가지만 바꿉니다. 자세한 절차는 `/harness`의 "F. 회고"에 있습니다.

### 실패했을 때

- **error**: 실패 기록만 `chore(...): step N failed`로 커밋되고 코드 변경은 작업 폴더에 남습니다. 확인 후 `phases/<task>/index.json`에서 해당 step의 `status`를 `"pending"`으로 바꾸고 `error_message`를 지운 뒤 다시 실행합니다.
- **blocked** (API 키 등 사람이 필요한 경우): 사유를 해결하고 `status`를 `"pending"`으로, `blocked_reason`을 지운 뒤 다시 실행합니다.

## 주의사항

- step 세션은 `--dangerously-skip-permissions`로 실행됩니다(사람 확인 없이 명령 실행). 위험 명령 차단 hook은 **패턴 기반의 최소 안전장치**일 뿐 완전한 격리가 아닙니다. 중요한 환경에서는 컨테이너나 VM 안에서 실행하세요.
- hook은 명령 전체 문자열을 검사하므로, 커밋 메시지나 데이터 안에 위 패턴이 들어 있어도 차단될 수 있습니다.
- 프롬프트가 30,000자를 넘으면 경고가 나옵니다. 매 step에 `CLAUDE.md`와 `docs/*.md` 전체가 들어가기 때문이며, 경고가 나오면 문서를 줄이거나 나누는 것을 검토하세요.

## 하네스 자체 개발

```bash
python3 -m pip install pytest
python3 -m pytest scripts/ -q
```

PR과 `main` push 때 GitHub Actions가 macOS·Linux·Windows × Python 3.9/3.13에서 같은 테스트를 실행합니다.
