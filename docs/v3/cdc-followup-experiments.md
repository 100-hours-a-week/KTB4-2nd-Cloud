# CDC 장애 실험 도구 사용법

`prepare-v3-cdc-followup.py`는 계획 생성과 실제 주입을 분리한다. 병합만으로 설치·장애 주입·GPU 기동이 실행되지 않는다. 실험 시나리오·시간 목표·실행 시간은 별도로 검토하고 승인한다.

## 명령

| 명령 | 동작 |
| --- | --- |
| `snapshot` | 배포·Pod UID·Endpoint·Kafka 타이머 설정의 읽기 전용 대조 |
| `plan` | C0 연속 MySQL / C1 간헐 MySQL / C2 Kafka 출력 차단의 비활성 계획 생성 |
| `run` | 검토한 Plan 해시와 협조 확인을 요구하는 제한된 주입·복구 |
| `collect` | 지정 시험 여행의 item·attachment·시도 횟수·receipt·Trace 메타데이터 조회 |
| `observe` | 동일 여행의 읽기 전용 DB 반복 관측, 제한된 timeout 재조회와 공백 기록 |
| `reconcile` | 접수·최종 ID 집합과 같은 Trace의 연결 범위 대조 |
| `assess` | 시간·최종 상태·장애 노출·수동 조치·관측 공백을 구분한 결과 작성 |

각 명령의 인자는 `python3 scripts/prepare-v3-cdc-followup.py <명령> --help`로 확인한다. 출력은 새 파일과 권한700의 디렉토리만 사용한다. 운영 조회 및 실험 증거는 Git에 저장하지 않는다.

## 실행 계약

- 기존 TLS 검증 EKS 연결과 localhost Prometheus tunnel 사용. snapshot은5분 내 재검증하고 변경된 UID·배포·설정에는 새 계획 필요.
- 주입 source는 Connect Pod 하나. destination은 검토한 MySQL3306 또는 Kafka Broker·bootstrap의9093만 허용. Kafka 차단은 업무 Topic뿐 아니라 config/status/offset/history 및 group 통신에도 영향을 준다.
- 최초 활성 작업 경보가 이미 pending/firing이면 새 탐지 측정을 시작하지 않는다. SQL sample·scrape·rule 평가가 신선하지 않으면 주입 전 차단하고 주입 중에는 finally 복구한다.
- C1은 현재 배포의 완전한 C0 결과가 필요하다. 회복 창 내 시험 작업의 실제 발행이 없으면 재주입하지 않는다. 두 번째 장애에 미완료 업무가 없으면 두 번의 업무 복구 성공으로 표시하지 않는다.
- 각 phase receipt의 UID/spec으로 본인이 만든 장애만 복구한다. 중간 phase는 AllRecovered 확인 후 API precondition을 사용해 삭제하고 마지막 리소스는 paused로 보존한다. 강제 삭제·finalizer 제거·offset/history 초기화는 하지 않는다.
- GPU 수명주기와 Task 재시작 API는 없다. 실제 입력과 주입은 협조 시간 안에서 별도 수행한다.

## 결과 해석

`assess`의 timing 입력은 timezone이 있는 UTC `firstActiveCreatedAt`, `firstFiringEvaluationAt`, `releaseObservedAt`, `firstDeliveryObservedAt`, `completedAt`과 각 근거 파일을 담은 `sources`를 요구한다. `faultWindows`에는 phase receipt의 `controllerInjectionObservedAt`을 `injectedObservedAt`으로, `recoveryRequestedAt`을 같은 이름으로 기록한다. `manualActions`와 `observationGaps`는 명시 배열이다.

DB created_at은 정확한 commit 시각이 아니며 publication receipt는 Broker 도착의 정밀 측정이 아니다. Controller의 AllInjected/AllRecovered도 패킷 경계와 다르다. SQL 시작/완료·scrape timestamp·rule 평가 시각을 구분하고 DB/observer clock 오차를 별도 보고한다. firing은 외부 알림 수신 증거가 아니다.

Kafka 증거는 BE receipt listener의 전달 위치이며 raw Broker 소비 횟수는 미측정으로 남긴다. 사진 행 수와 idempotent Producer만으로 모든 외부 부작용 중복0 또는 end-to-end exactly-once를 주장하지 않는다. 고정 Kafka 버전의 [Connect Worker](https://github.com/apache/kafka/blob/4.2.2/connect/runtime/src/main/java/org/apache/kafka/connect/runtime/Worker.java)가 source Producer의 대기 설정을 덮어쓰므로 일반 Producer 기본 timeout을 그대로 적용하지 않는다.

## DB 관측의 일시 오류

저장소 루트에서 아래 명령을 별도 프로세스로 실행한다. 기존 TLS 검증 kubeconfig와 미리 생성한 시험 여행 ID를 사용하며, 이 명령은 장애·GPU·업무 변경을 실행하지 않는다. 실제 조회 전 여행 메타데이터 접근 범위를 확인한다.

```sh
umask 077
mkdir -m 700 /tmp/cdc-db-observation
python3 scripts/prepare-v3-cdc-followup.py observe \
  --kubeconfig "$KUBECONFIG" --trip-id '<시험 여행 ID>' \
  --directory /tmp/cdc-db-observation --seconds 780 --ack-trial-metadata-read
```

- 먼저 정상 sample과 `status.json`의 ready를 확인한 뒤, 기존 `run` 명령에 `--db-observation-directory /tmp/cdc-db-observation`을 추가한다. 같은 여행·승인 Revision·30초 이내 상태만 허용한다. 다른 여행이나 오래된 경로를 재사용하지 않는다.
- 기존 DB 호출 20초 제한을 유지하고 subprocess timeout에만 1초 후 1회 재조회한다. Pod UID 재확인과 backoff를 포함한 표본 전체 예산은 45초이며 남은 예산으로 각 호출을 제한한다. SQL은 기존 read-only transaction·SELECT 3초 제한·행 상한을 유지한다. 권한·대상 변경·잘못된 응답은 재조회하지 않는다.
- `--seconds`는 준비·주입·복구를 포함한 이 관측 프로세스의 전체 실행 예산(최대960초)이다. 해제 후780초 진단 상한과 시작점이 다르므로 실험 명세에서 구분한다. 예산 도달은 완료로 처리하지 않는다.
- 첫 실패부터 `blocked.json`을 보존한다. 관측은 재조회 성공 뒤에도 계속하지만 추가 주입은 다시 허용하지 않는다. 연결한 `run`은 이를 감지해 기존 finally 경로로 본인 장애를 복구한다. 이후 해당 시도는 중단/공백을 포함한 결과로 기록하며 정상 길이의 재시험과 합치지 않는다.
- 이 interlock은 장애 실행기의 기존 Prometheus 신선도 검사에 추가된다. 관측 프로세스 종료·상태 누락·30초 초과도 실패로 처리한다. 독립 GPU 종료 가드를 대체하거나 GPU를 직접 중지하지 않는다.
- attempt별 실패·회복·표본 파일은 새로만 생성하고 status만 원자적으로 교체한다. 원문 명령·stdout/stderr·비밀을 오류 기록에 넣지 않는다. `summary.json`과 각 sample의 시각·DB clock 범위를 보존한다. 브라우저 접수·Trace·지표 수집은 별도 프로세스를 유지한다.

판정 시 기존 `assess`에 `--db-observation-summary /tmp/cdc-db-observation/summary.json`을 함께 전달한다. 같은 여행·배포인지 확인한 뒤 gap과 요약 해시를 timing에 연결한다. 재조회로 완료 시각을 회수해도 gap을 지우지 않아 strict evidenceComplete/automaticRecovery를 성공으로 승격하지 않는다. 최종 DB 상태는 sample의 execution_timing, 접수·최종 ID 대조는 기존 reconcile로 각각 확인한다.

읽기 실패가 반복되면 관측 명령은 오류 종료한다. 추가 입력과 주입은 중단하고 소유 장애의 복구를 확인한다. 사후 읽기로 완료를 회수하더라도 수동 업무 복구와 구분하고, 원래 오류·공백·판정 파일을 덮어쓰지 않는다.
