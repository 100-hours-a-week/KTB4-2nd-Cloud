# CI 실행 시간 수집

네 저장소의 PR Gate 실행 메타데이터를 읽고 로컬에서 분석한다. Python 3.10 이상과 인증된 GitHub CLI가 필요하다. 별도 Python 패키지, AWS 로그인, Docker 실행은 필요하지 않다.

Cloud 저장소에서 실행한다.

```bash
gh auth status
python3 scripts/ci-baseline.py collect \
  --output .local-docs/evidence/ci-baseline-YYYYMMDD-HHMMSS \
  --limit 20
```

새 디렉터리 이름을 사용한다. 기존 디렉터리에 수집을 덮어쓰지 않는다. 저장소 하나만 필요하면 `--repository AI`를 사용한다. 반복 지정할 수 있고 기본값은 FE/BE/AI/Cloud 전체다. limit은 저장소별 최근 Run 수이며 1~100을 허용한다. 해당 Run의 접근 가능한 이전 attempt도 수집한다.

`gh api --method GET`만 사용하며 Workflow 실행·재실행·취소·배포나 저장소 설정 변경은 하지 않는다. 인증값은 CLI가 처리하고 도구는 토큰이나 API 오류 본문을 저장·출력하지 않는다. 원본은 실행 메타데이터와 Workflow 코드이며 애플리케이션 로그나 Secret을 읽지 않는다.

일부 조회가 실패하면 종료 코드는 1이다. manifest의 complete/errors와 report의 Collection incomplete를 확인한다. 이미 받은 자료는 보존하고 새 수집은 별도 디렉터리로 실행한다. 성공 0이어도 캐시 상태와 전체 PR 변경 범위를 검증했다는 뜻은 아니다.

네트워크 없이 같은 자료를 다시 분석할 수 있다.

```bash
python3 scripts/ci-baseline.py analyze \
  --output .local-docs/evidence/ci-baseline-YYYYMMDD-HHMMSS
```

## 결과 파일

| 파일 | 내용 |
| --- | --- |
| manifest.json | 수집 시각, 저장소별 완료 여부·오류, attempt 파일 목록 |
| analysis-metadata.json | 분석 시각·분석 코드 해시와 수집 코드 해시. 이전 Snapshot에 없는 수집 해시는 null |
| FE 등 저장소 디렉터리 | Run 목록, attempt별 Run/Jobs, source SHA의 Workflow와 해당 Commit 파일명 |
| runs.csv / summary.json | 전체 구간, Runner 합계, 결과·attempt, 실행/skip Job, Workflow 해시 |
| jobs.csv | Job별 실행 시간·시작 오프셋·해석 가능한 선행 조건 이후의 지연 |
| steps.csv | Step별 결과와 실행 시간, skipped는 null |
| groups.json | 첫 attempt 성공 중 Workflow·실행 Job·검증 Step·head Commit 분류·캐시 상태가 같은 그룹의 중앙값·범위 |
| report.md | 성공·실패·취소·시간 불완전 attempt 수와 해석 주의점 |

전체 시간은 첫 attempt에서는 Run 생성부터 마지막 실제 실행 Job 종료까지다. 재실행은 해당 attempt의 run_started_at부터 측정해 최초 PR 생성 이후 며칠의 간격을 대기 시간으로 합산하지 않는다. 진행 중이거나 실제 실행 Job의 시간이 불완전하면 전체 구간을 null로 둔다. 실패·취소 실행도 확인 가능한 시간과 원본/CSV를 남기며 성공 성능 그룹에서는 분리한다.

Runner 합계는 중첩 실행 Job 시간을 더한 관측값이며 청구액이 아니다. 실행 중인 Job의 아직 닫히지 않은 구간은 포함하지 않으므로 timing_complete를 함께 확인한다. skipped Job의 API 타임스탬프를 실행 시간으로 환산하지 않는다.

## 범위와 해석

Workflow는 최신 main 하나를 과거 모든 실행에 적용하지 않고 각 Run의 head_sha에 있는 파일을 보존한다. 이는 PR head의 구성 Snapshot이며 실제 merge checkout SHA는 이 API만으로 확정하지 않아 checkout_sha=null로 둔다. Base 브랜치의 Workflow 변경이 함께 실행 구성에 영향을 줬다면 원본과 실행 내용을 추가 대조한다.

의존 관계는 현재 네 Workflow의 literal jobs/name/needs 문법만 해석한다. Matrix·동적 표현식·재사용 Workflow·순환 관계·Job 이름 불일치는 dependency_graph_known=false로 둔다. 이때 ready_to_start_seconds도 null이다. 복잡한 YAML을 완전하게 해석하는 Parser는 아니다.

해석 가능한 경우 Root Job은 측정 원점, 종속 Job은 선행 Job 종료 시각 중 최댓값을 기준으로 시작 지연을 계산한다. 이 지연이 GitHub Runner 부족 때문인지 등 원인을 자동 판정하지 않는다. observed_completion_chain은 마지막 종료 Job에서 늦게 끝난 선행 Job을 역으로 따라간 경로이며, 실제 검사 내용과 함께 확인한다.

head_commit_change_kind는 **해당 Commit의 파일 변경** 분류다. 전체 PR diff를 뜻하지 않는다. Commit 파일 목록이 100개에 도달해 잘릴 수 있으면 unknown으로 남긴다. PR 전체 분류·cache_state는 별도 증거가 없어 unknown이다. Step 시간이 짧다는 이유로 cache hit로 추정하지 않는다.

first_verification_step_seconds는 Step 이름으로 Lint/Typecheck/Test/Build 등 최초 종료를 고른 값이다. 성공 단계가 검증을 끝까지 수행했는지 원본 Step와 대조해야 한다. first_failure_seconds는 준비 단계의 실패도 포함한다. OS Label은 보존하지만 실제 vCPU/메모리는 별도 확인 대상이다.

groups는 관측 이력 분석용이며 통제된 전후 Benchmark가 아니다. 표본 20건 미만은 p90=null이다. 개선 비교에서는 동일한 Source·Runner 조건과 확인된 캐시 상태, 필요한 검증 목록을 맞춘 별도 실험을 사용한다.

계산 검증은 다음 명령으로 실행한다.

```bash
python3 -m unittest discover -s tests/ci -p 'test_ci_baseline.py'
```

관련 API: [Run attempt별 Job](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt), [Workflow Runs](https://docs.github.com/en/rest/actions/workflow-runs).
