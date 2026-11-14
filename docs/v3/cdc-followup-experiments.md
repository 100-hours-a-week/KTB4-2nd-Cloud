# CDC 장애 실험 도구 사용법

`prepare-v3-cdc-followup.py`는 계획 생성과 실제 주입을 분리한다. 병합만으로 설치·장애 주입·GPU 기동이 실행되지 않는다. 실험 시나리오·시간 목표·실행 시간은 별도로 검토하고 승인한다.

## 명령

| 명령 | 동작 |
| --- | --- |
| `snapshot` | 배포·Pod UID·Endpoint·Kafka 타이머 설정의 읽기 전용 대조 |
| `plan` | C0 연속 MySQL / C1 간헐 MySQL / C2 Kafka 출력 차단의 비활성 계획 생성 |
| `run` | 검토한 Plan 해시와 협조 확인을 요구하는 제한된 주입·복구 |
| `collect` | 지정 시험 여행의 item·attachment·시도 횟수·receipt·Trace 메타데이터 조회 |
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
