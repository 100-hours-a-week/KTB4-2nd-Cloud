# 사진 변환 lease 비교 준비 (W2K)

기본 비활성. 병합만으로 배포·사진 접수·GPU 기동·장애 주입을 하지 않는다.
기존 W1/W1K의 고정 이미지·600초 정책·엄격한 사진 판정과 원본 결과는 보존한다.
W2K는 새로 사전 등록하는 비교이며, 과거 W1K를 재판정하는 옵션이 아니다.

## 준비 순서

1. BE 정책 지원 코드의 CI·병합·이미지 발행 확인. **같은 지원 이미지**로 600초 기준선과 단축 정책을 비교한다.
2. 로컬에 있는 정확한 Digest 이미지의 JAR를 정지 컨테이너에서 검사한다. pull·실행·운영 연결은 하지 않는다.
3. 기존 승인 후보에서 converter 설정 두 항목만 추가한 600초 후보를 만든다. 기존 이미지·사양·APM·HPA/KEDA 보존.
4. 후보 전용 PR·필수 CI·GitHub 환경 승인으로 배포한다. CI도 실제 후보 이미지의 정책 지원 구조를 검사한다.
5. 새로운 승인 Snapshot revision으로 review와 snapshot/Plan을 만들고, 같은 JPEG20·지역·입력 방식과 container-kill로 기준선을 측정한다.
6. 갱신 간격과 DB 갱신 시간의 실측값에 별도로 설명한 정지 여유를 더해 후보를 정한다. 120초는 확정값이 아니다.
7. 같은 이미지에서 lease만 변경해 동일 절차로 재시험하고, 두 실제 결과와 조건 파일을 비교한다.

준비 중 GPU는 켜지 않는다. 실제 시험의 요청 중단·GPU 기동·종료 가드·한시 권한 범위는 실행 전 별도로 확인한다.
단축 후보를 만들기 전 관측이 없거나 예산을 넘지 못하면 진행을 차단한다. 값/정책을 결과에 맞춰 소급 변경하지 않는다.

## 설정과 예산

- BE 환경변수: `ATTACHMENT_CONVERSION_LEASE_DURATION=600s`, `ATTACHMENT_CONVERSION_LEASE_RENEW_INTERVAL=30s`.
- BE 기본값은 600초·30초이며 선점과 갱신에 같은 정책을 사용한다. lease는 최소 갱신 주기의 3배를 요구한다.
- Cloud 비교 범위는 converter 하나의 lease 90~600초와 갱신 30초 고정이다. 다른 역할에 주입하거나 한 항목만 넣을 수 없다.
- 후보는 `max(90, 2 × (max(30, 실측 갱신 간격 최댓값) + 실측 DB 갱신 시간 최댓값) + 설명한 미관측 정지 여유)`보다 길어야 한다.
- 두 배 여유와 추가 정지 여유는 명시적인 공학적 선택이며 통계적 SLO가 아니다. CPU 정지·GC·DB 지연의 미관측 꼬리와 native IO를 검토해야 한다.
- Micrometer max는 rolling maximum이다. 고정 수집 구간의 여러 표본을 대조하고 all-time max나 p99로 표현하지 않는다.
- 주기 heartbeat와 사진 처리 경계의 모든 갱신을 분리해 관측한다. 새 Task의 갱신 간격은 이전 Task와 합치지 않는다.

## 오프라인 도구

출력은 권한700 디렉터리의 새 파일만 허용한다. 아래 대문자 값은 검토한 실제 파일/메타데이터로 지정한다.

```sh
python3 scripts/prepare-v3-conversion-lease.py inspect \
  --image IMAGE_DIGEST --source BE_SOURCE_SHA --output PRIVATE/support.json
python3 scripts/prepare-v3-conversion-lease.py candidate \
  --candidate APPROVED_CANDIDATE --support PRIVATE/support.json \
  --duration-seconds 600 --output PRIVATE/baseline-candidate.json
```

`inspect --jar LOCAL_JAR`는 로컬 컴파일 호환 증거다. 이 결과는 이미지 mode가 아니므로 배포 후보 준비에 사용할 수 없다.
이미지 검사도 구조 호환 증거이며 실제 BE DB/소유권 회귀와 운영 복구 성공을 대신하지 않는다.

정상 배포 후 아래 review를 만들고 기존 worker 도구의 `snapshot --lease-review PRIVATE/review.json`에 전달한다.
기존 daemon/profile/sampling-probe/plan/run/원복 경로를 그대로 사용한다. 새 case는 W2K이며 600초와 단축 정책 모두 관찰 창900초를 유지한다.

```sh
python3 scripts/prepare-v3-conversion-lease.py review \
  --candidate PRIVATE/baseline-candidate.json --support PRIVATE/support.json \
  --approved-revision APPROVED_SNAPSHOT_SHA --output PRIVATE/review.json
```

기존 `collect-v3-experiment.py collect` 인자에 `--include-conversion-lease --lease-snapshot PRIVATE/snapshot.json`을 추가한다.
9개 고정 lease 쿼리는 정확한 피해 Pod만 조회한다. 없는 시계열/조회 오류/비유한 값은 정상0으로 대체하지 않는다.

```sh
python3 scripts/prepare-v3-conversion-lease.py budget \
  --metrics PRIVATE/baseline-metrics/result.json --pause-allowance-seconds REVIEWED_MARGIN \
  --rationale '측정 구간과 CPU/GC/DB 지연의 미관측 여유를 설명' --output PRIVATE/budget.json
python3 scripts/prepare-v3-conversion-lease.py candidate \
  --candidate PRIVATE/baseline-candidate.json --support PRIVATE/support.json \
  --duration-seconds REVIEWED_LEASE --budget PRIVATE/budget.json --output PRIVATE/short-candidate.json
```

단축 후보 배포 후 새 approved revision으로 review를 다시 만든다. 단축 review에도 동일 budget 파일이 필요하다.
W2K 판정에는 `prepare-v3-conversion-lease.py assess --plan PLAN --directory EXECUTION --trace TRACE --metrics METRICS --output RESULT`를 사용한다.
비교에는 `compare --baseline RESULT_A --candidate-result RESULT_B --baseline-metrics METRICS_A --candidate-metrics METRICS_B --output COMPARISON`을 사용한다.

## 판정과 증거

- 주입 전에 부분 저장된 Task와 기존 claim·Kafka 소유권을 고정한다.
- 종료137(Error, OOM 제외)과 정확한 이전 containerID를 확인한 **뒤 첫 DB 조회**에서 같은 Task의 시작됐으나 미저장인 사진 전체를 고정한다.
- 집합이 비어 있거나 기존 claim/유효 lease가 없으면 해당 조건 미포착이다. 단일 현재 사진의 item/attempt/attempt-owner가 기존 claim과 맞아야 하며, 모호한 과거 미저장 시도가 섞이면 성공 판정을 차단한다. 주입 뒤 조건 미포착이어도 업무 관찰과 원복은 계속한다. 결과를 보고 사진을 골라 성공으로 바꾸지 않는다.
- 집합 전체의 재시도·저장, 이미 저장된 사진의 hash/ID/attempt 보존, 최종 COMPLETED와 DB 결과 유일성을 대조한다.
- 새 Kafka 멤버·만료/재선점·최종 완료를 구간으로 계산하고, 수동 초기화·조기 재선점·관측 공백·Trace 미확보는 성공 판정을 차단한다.
- 구형 assess 명령은 W2K를 거부한다. 새 결과에도 구형 사전 지목 판정을 별도로 남겨 과거 기준을 감추지 않는다.
- 재선점15초·재선점 후 완료240초 목표는 유지한다. 종료→완료의 공학적 목표는 `max(lease, 기존 cold readiness 여유180초)+15+240+30`이다. 관찰 창은 두 정책 모두900초로 유지한다.
- 동일 이미지·입력·정책 외 후보 해시·Pod 자원·Node 종류·확장 정책을 대조한다. 조건 차이가 있거나 두 결과가 불완전하면 인과적 개선 비교를 하지 않는다.
- DB 결과 유일성은 exactly-once 또는 모든 S3 부작용 부재의 증거가 아니다. 늦은 S3 결과·정리의 보호는 BE 회귀와 별도 증거로 남긴다.
- 수치와 실제 Grafana의 lease·갱신·Ready·처리 단계·Trace 캡처를 확보하고, GPU는 업무 관찰 종료 즉시 중지한다.
- 600초로의 정책 복원도 승인형 배포로 진행한다. 기존 lease/offset/history를 강제로 초기화하지 않는다.
