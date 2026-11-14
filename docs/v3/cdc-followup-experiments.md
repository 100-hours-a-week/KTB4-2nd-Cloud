# CDC 탐지 시간과 후속 장애 실험

상태: #694 구현. **운영 적용·장애 주입 전**. 병합 자체는 설치·주입·GPU 기동을 실행하지 않음.

## 질문과 순서

202로 접수한 작업이 전달 경계의 반복 장애에도 보존되고, 재전달이 사용자 결과의 중복으로 이어지지 않는가?

- C0: 현 성남 사진 20장 조건에서 Connect→MySQL TCP3306을 설정상 90초 차단. 새 탐지 경보와 현재 배포의 대조군.
- C1: C0가 근거를 갖춘 자동 복구 결과일 때만 45초 차단→회복 창→45초 차단. 누적 차단은 90초지만 전체 길이·회복 위상은 C0와 다름. 단일 변수의 성능 개선율 실험이 아님.
- C2: Connect→세 Broker와 bootstrap Service의 TLS9093 차단. Broker 자체·다른 클라이언트·9090/9091은 주입 대상 아님. 업무 Topic뿐 아니라 config/status/offset/history 및 Connect group 통신도 영향받음.
- 그다음 Worker 부분 처리 중 종료 실험. CDC 주제는 C0 한 번의 성공으로 끝내지 않음.

고정 입력: 성남, 동일 JPEG20장(40,302,057 bytes), 동시 여행 1건. 입력 해시·현재 승인 Snapshot·HPA/KEDA·Pod/Node 사양을 고정. 캐시를 비우지 않은 정상 3회는 클릭→DB 완료 199.494344 / 200.206262 / 194.034593초. 최대 200.206262초는 설명 표본이며 p95·최악 상한·서비스 SLO가 아님. 현재 기준의 Outbox created_at→published_at 최대 0.927444초도 commit→Broker 도착의 정밀 측정이 아님.

## 목표의 근거와 한계

| 구분 | 목표 초안 | 근거 | 성격 |
| --- | ---: | --- | --- |
| 활성 미발행 작업→firing | 60초 | 연령15 + SQL 후 sleep5 + scrape15 + 평가15 + 여유10 | 정상 발행 약0.93초 대비 연령 여유. 조회/스케줄링이 늘면 넘을 수 있는 실험 목표 |
| C0/C1 마지막 해제 관측→첫 전달 receipt | 30초 | keepalive10초, 이전 단일 재시험 첫 발행10.492초 | 현재 조건 C0에서 확인할 목표. TCP 복구 보장 아님 |
| C0/C1 해제→최종 완료 | 270초 | 정상 최대를210으로 올림 + 전달30 + 변동 여유30 | 30초 여유는 정책 선택. 통계적 신뢰구간 아님 |
| C0/C1 자동 복구 관찰 상한 | 360초 | 완료 목표270 + 진단90 | 초과 시 실패/진행 중/판정 불가를 보존하고 수동 조치 분리 |
| C1 회복 창 | C0 이후30~60초 | C0 실측 전달 시간×2를10초 단위 올림, 최소30 | C0 전달30초 목표 미충족이면 자동 연장하지 않고 계획 재검토 |
| C2 첫 전달 | 60초 | Kafka request.timeout 기본30초의 두 창을 초기 운영 허용 목표로 선택 | 아직 C2 실측 없음. 재시도 최대 시간에서 유도한 보장 아님 |
| C2 최종 완료 | 300초 | 정상210 + 전달60 + 변동30 | C0의270초를 복사하지 않음 |
| C2 진단 관찰 상한 | 780초 | unsync300 + scheduled rebalance300 + group join60 + request30 + offset interval60 + 여유30 | 이 경로들은 병렬·조건부일 수 있음. 모두 직렬로 발생한다는 예측이나 복구 보장이 아님 |

**목표와 관찰 상한은 다름.** 60초 목표를 넘었는데 780초 안에 살아났다면 목표 미달 자동 복구로 기록. 모든 값은 실행 전 사용자와 협의. 비용이 허용되지 않으면 C2 상한을 새 계획으로 줄이고, 긴 내부 backoff 경로를 끝까지 확인하지 못했음을 기록. 장애 주입 시간90초는 과거와 비교 가능한 제한된 교란 예산이지 실제 서비스 중단 시간을 보장하는 값이 아님.

SQL 단일 벽시계 측정0.214509초는 CPU 사용률·p95·3배 조회 빈도의 부하 보장이 아님. SQL 자체 최대 실행 힌트, CLI8초 제한,10,001행 집계 상한·최소 권한은 보존. 계측 시작/완료 시각을 별도 노출하여 sleep5초 외에 실제 조회 시간이 더해지는 효과를 측정. 수집기 status조회3초와 scheduling은60초 보장을 깨뜨릴 수 있으므로 초과를 숨기지 않음.

## Kafka 고정 버전의 중요한 차이

저장소 고정 Kafka4.2.2의 [Worker.baseProducerConfigs](https://github.com/apache/kafka/blob/4.2.2/connect/runtime/src/main/java/org/apache/kafka/connect/runtime/Worker.java)는 일반 Producer 기본값을 그대로 쓰지 않음. source Producer의 max.block.ms=Long.MAX_VALUE, delivery.timeout.ms=Integer.MAX_VALUE를 먼저 설정하고 worker의 producer.*와 Connector override를 적용. 따라서 일반 Producer의 delivery.timeout120초로 Task 실패/복구를 예상하면 틀림. 현재 worker는 idempotence=true와 acks=all을 명시하지만 이 사실만으로 재시작·offset replay에 대한 end-to-end exactly-once가 증명되지는 않음.

[DistributedConfig](https://github.com/apache/kafka/blob/4.2.2/connect/runtime/src/main/java/org/apache/kafka/connect/runtime/distributed/DistributedConfig.java)의 unsync backoff와 scheduled rebalance 최대 지연은 각각300초. [Connect 설정](https://kafka.apache.org/42/configuration/kafka-connect-configs/)의 heartbeat/session/join과 offset flush를 분리. Debezium MySQL keepalive와 Kafka group heartbeat는 다른 연결임.

C2 snapshot은 KafkaConnect 버전·이미지 digest·숫자 타이머 override·config 해시와 Connector config 해시를 보존. 해시는 Secret 값의 출력이 아니며 kubeconfig/CR 전체는 저장하지 않음. 이는 **고정 소스 기본값+CR override 검토**이며 실행 JVM 전체의 설정 덤프가 아님. Strimzi 생성 설정이나 JVM 옵션이 이 계산을 덮어쓴 사실이 드러나면 C2 계획을 갱신해야 함. binlog reader가 살아 있어도 출력 backpressure 때문에 계속 읽는다고 가정하지 않음.

## 시간 축과 증거

1. DB created_at: 첫 활성 Outbox 기록. 정확한 commit 시각과 구분.
2. SQL started/completed/duration: wall clock, CPU 측정 아님. 기존 snapshot 형식도 읽을 수 있어 rolling 적용 중 호환.
3. Prometheus 실제 sample timestamp: instant query 평가 시각과 구분해 timestamp()로 수집.
4. rule lastEvaluation/state/alerts: pending activeAt을 firing 시각으로 대체하지 않음. 별도 외부 알림 수신 확인은 이번 구현에 없음.
5. NetworkChaos 활성화 요청/AllInjected 첫 관측/복구 요청/AllRecovered/최초 전달/완료를 각각 보존. Controller 조건은 패킷 경계의 정밀 시각 아님.
6. accepted item 집합→final item의 attachment 연결→활성 attachment/file 집합, conversion_attempts, Outbox ID/topic/partition/offset/publicationVersion/traceparent, creation 최종 상태를 대조.
7. 기존 APM 수집기의 no_observed_loss와 실제 같은 Trace ID 연결을 함께 확인. 다른 Trace의 성공을 끌어오지 않음.

현재 Kafka 증거는 BE receipt listener가 DB에 기록한 전달 위치. raw Broker 메시지 소비 횟수는 미측정이며 null로 남김. 모든 외부 부작용의 중복0이나 exactly-once를 주장하지 않음. 장소 분류 개수는 모델 결과라 동일 개수 강제하지 않음.

## 실행 절차

아래는 사용법이며 실행 승인 아님. 사전 요청 중단, 입력량·주입 조건·시간 협의와 읽기 범위 확인 필요. GPU는 실험 창에서만 별도 기동하고 관찰 종료 직후 중지·stopped 확인. 도구에는 GPU/Task 재시작·offset/history 삭제 API 없음.

```sh
umask 077
mkdir -m 700 /tmp/cdc-694-plan
# 기존 TLS 검증 EKS 연결과 localhost Prometheus tunnel 사용.
python3 scripts/prepare-v3-cdc-followup.py snapshot \
  --kubeconfig "$KUBECONFIG" --controller-node '<기존 승인 hostname>' \
  --allow-worker-controller --output /tmp/cdc-694-plan/snapshot.json
python3 scripts/prepare-v3-cdc-followup.py plan --case C0 --run-id cdc-c0 \
  --snapshot /tmp/cdc-694-plan/snapshot.json --output /tmp/cdc-694-plan/plan.json
# 계획·대상·해시 검토 후에만 run. 미리 만든 여행 ID를 사용하되 사진 접수는 차단 관측 후 수행.
mkdir -m 700 /tmp/cdc-694-run
python3 scripts/prepare-v3-cdc-followup.py run \
  --kubeconfig "$KUBECONFIG" --plan /tmp/cdc-694-plan/plan.json \
  --approve-plan-sha256 '<검토한 해시>' --ack-shared-cdc-maintenance \
  --prometheus http://127.0.0.1:19090 --directory /tmp/cdc-694-run --trip-id '<시험 여행 ID>'
```

- snapshot은5분 내 재검증. broker3개의 PodUID/IP/owner와 bootstrap EndpointSlice 및9093을 확인. source는 Connect Pod 하나만, destination은 정확한 IP:port만. 변경된 UID·배포·config는 새 계획 필요.
- run은 새 파일/700폴더만 사용. 최초 활성 작업 경보가 이미 pending/firing이면 새 탐지 측정으로 시작하지 않음. SQL sample·scrape·rule 평가가 신선하지 않으면 주입 전 차단, 주입 중에는 finally 복구. 관측 실패를 정상0으로 바꾸지 않음.
- C1 첫 phase의 AllRecovered 확인 후 본인이 생성한 UID/spec의 paused 리소스만 API precondition으로 삭제. 다음 phase 전에 동일 시험 Outbox의 회복 창 내 실제 발행을 요구. 없으면 재주입 차단. 마지막 리소스는 paused로 남겨 별도 회수 검토.
- 설정상 회복 창과 실제 두 활성화 사이 간격은 다를 수 있음. API/복구 대기 시간을 포함한 실측 간격을 함께 기록. C1 두 번째 차단에 미완료 시험 업무가 전혀 없으면 두 번의 업무 복구 성공으로 표시하지 않음.
- `collect --ack-trial-metadata-read`는1건의 item/attachment/receipt/Trace 메타데이터만 조회. 원문 사진·파일명·위치·사용자·payload는 내보내지 않음.
- `reconcile`은 ID/receipt/Trace의 범위만 판정. `assess`는 accepted/final/기존 APM trace-evidence와 별도 timing JSON을 받아 최종상태·주입 노출·시간·수동 조치 유무를 구분. 원본4종 해시를 결과에 기록.
- C1 plan의 `--c0-result`에는 현재 승인 배포의 완전한 C0 assess 결과만 사용. 임의의 대기 시간 하나를 전달하는 방식은 없음.

`timing` 파일: UTC aware `firstActiveCreatedAt`, `firstFiringEvaluationAt`, `releaseObservedAt`, `firstDeliveryObservedAt`, `completedAt`; 각 키의 근거 파일을 `sources`에 기록. `faultWindows`는 각 phase receipt의 `injectedObservedAt`(controllerInjectionObservedAt)·`recoveryRequestedAt`. `manualActions`와 `observationGaps`는 명시 배열. 누락·clock 역전·다른 Trace·다른배포·데이터 잘림은 완전한 증거로 판정하지 않음. DB DATETIME UTC와 observer clock 오차는 별도 측정/한계로 보고하며 밀리초 정밀도만으로 정확도를 주장하지 않음.

## 포트폴리오에서 사용할 판단

V2 이전의 접수 요청 보존→Nginx의 정상 사용자 영향과 회복→V3에서 Task RUNNING이어도 전달이 멈출 수 있다는 관찰로 연결. 단순 Pod 재생성이 아니라 upstream DB 적체, downstream Kafka 가시성, Probe/Strimzi·KEDA/HPA·Karpenter 책임 경계와 업무 최종 결과를 분리해서 판단. 실패/판정 불가·수동 복구도 결과이며 목표를 넘긴 뒤 기준을 늘려 성공 처리하지 않음.
