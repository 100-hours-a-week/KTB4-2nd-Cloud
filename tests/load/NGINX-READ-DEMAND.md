# V1 정상 조회 호출 실측 — #416

기존 단일 상세 조회149회/평균0.49RPS는 k6가 정한 주기의 결과다. 공통 조회 제한의
rate·burst 근거로 쓰지 않는다. 이번에는 실제 V1 FE 화면의 지도·검색·자동 페이지
조회·SSR 요청이 얼마나 짧게 몰리는지 측정한다. 공격 재현이나 서버 용량 시험이 아니다.

대상은 `https://v1-staging.yeodam-2gether.com`이다. 기존 여행·사진을 사용한다.
V2 QA·운영·ProxySQL·DB 접속 설정·BE/FE 이미지와 업로드 제한을 변경하지 않는다.
발생기·AI Worker·공유 GPU 기동은 필요하지 않다. 새 여행 생성은 제출하지 않는다.

## 관측 적용과 확인 — Cloud 담당

`nginx_observability.py`는 기존 JSON format의 연결 필드를 유지하고 `msec`와
`peer_addr`만 추가한다. `peer_addr`는 Nginx의 `$remote_addr`이며 외부에서 입력한
X-Forwarded-For를 신뢰하지 않는다. raw IP는 기존 접근 로그 본문에만 저장되고
새 Prometheus/Loki stream label을 만들지 않는다. 기존14개 파일 회전·접근 경계와
Loki 보존 정책을 유지한다. 로컬 집계 결과는 실행별 HMAC 식별자로 익명화한다.
이는 서로 다른 실행의 같은 사용자를 추적하는 식별자가 아니다.

App에서 사전 상태와 site hash를 읽은 뒤 도구3개를 아래 디렉터리에 설치한다.

```text
/opt/yeodam/ops/nginx-read-measurement/
  install-nginx-read-measurement.py
  nginx_observability.py
  nginx-read-demand.py
```

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/install-nginx-read-measurement.py install \
  --expected-site-sha256 '<직전 읽기 전용 점검의 SHA256>'
```

Root와 V1 App 인스턴스 ID를 검사한다. 원본은 root 전용 백업에 보관하고 기존 파일의
log_format만 패치한다. `nginx -t` 후 graceful reload, 실패 시 원본 복구다.
출력된 before/after hash와 backup 경로를 기록한다. Docker/Alloy를 재시작하지 않는다.
임의 site 변경이 있으면 적용·원복을 거절한다. 원복도 현재 hash를 먼저 확인한다.

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/install-nginx-read-measurement.py rollback \
  --backup '<출력된 백업 디렉터리>' --expected-site-sha256 '<현재 SHA256>'
```

적용 후 짧은 헬스 확인으로 기존 JSON 파싱·새 필드·Loki 도착을 대조한다. 이는 정상
화면 측정 결과가 아니며 rate 선정 표본에 넣지 않는다. 전체 설정이나 환경변수·Cookie를
출력하지 않는다. 미래 staging renderer에도 같은 필드를 유지하고 운영 template은 바꾸지 않는다.

## 측정 터미널 — 사용자

로그 필드와 도구 설치 완료 안내를 받은 뒤 실행한다. **발생기가 아니라 V1 App에 접속**한다.

Mac 터미널:

```bash
aws ssm start-session --profile yeodam-admin --region ap-northeast-2 \
  --target i-05d320e3a87200d5a
```

SSM 안에서:

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/nginx-read-demand.py begin
```

출력되는 run_id와 UTC 시작 시각을 보관한다. 처음 단계 이름은 `single-1`이다.
이 명령은 서버에 요청을 보내거나 부하를 만들지 않고 측정 시간 경계만 기록한다.
최대30분 안에 끝내며 약5~10분의 탐색 표본을 목표로 한다.

## 브라우저에서 할 동작

V1 도메인에 로그인한 **평소 브라우저**를 쓴다. DevTools의 캐시 비활성화나 연결 제한은
켜지 않는다. 각 반복은 새로고침 후 시작해 QueryClient 메모리 캐시가 없는 진입을
포함한다. HTTP/S3 브라우저 캐시는 평소 설정을 유지하므로 완전한 cold-cache 실험이라고
주장하지 않는다. 다른 팀원 사용이 있다면 시작/종료 시각을 따로 적는다.

1. 홈 지도 진입 후 여행 하나를 선택한다.
2. 여행 목록에서 정렬·즐겨찾기 필터를 바꾸고 목록이 다 표시되는지 확인한다.
3. 기존 완료 여행의 폴더와 사진 목록을 열고 기다린다. 가능하면 사진이 많은 폴더를 선택한다.
4. 원본 사진5~10개를 빠르게 넘겨보고 뒤로 돌아온다.
5. 새 여행 만들기의 장소 검색까지 이동해 다른 검색어를 몇 번 입력한다. **제출하지 않는다.**

어느 목록이30초 넘게 계속 로딩하거나 오류가 보이면 해당 탭의 로딩을 중지하고
시각·화면·상태를 알려준다. 대량 데이터의 무한 연속 조회를 끝까지 강제로 진행하지 않는다.
이때는 불완전한 측정으로 남기고 정상 처리된 것으로 계산하지 않는다.

같은 순서를3번 수행한다. 두 번째·세 번째 시작 직전에 SSM 터미널에서:

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/nginx-read-demand.py mark --phase single-2
```

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/nginx-read-demand.py mark --phase single-3
```

마지막은 같은 네트워크의 탭3개를 새로 열어 지도·여행 목록·사진 목록에 거의 동시에 진입한다.
같은 브라우저/계정이면 **동일 사용자 다중 탭 측정**이지 독립 사용자3명의 실측이 아니다.
다른 기기/계정을 실제 사용했다면 그 조건을 별도로 기록한다.

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/nginx-read-demand.py mark --phase multi-tab
```

실제 진행 중 작업이 없으면 새 AI 작업을 만들지 않는다. Polling0.5RPS는 코드에서 확인한
모델로 분리한다. 나중에 동시 Polling을 합성해 비교해도 새 실측으로 표시하지 않는다.

## 종료와 결과 전달

화면 로딩이 끝나면 조작을 멈추고 터미널에서:

```bash
sudo python3 /opt/yeodam/ops/nginx-read-measurement/nginx-read-demand.py finish
```

추가5초 후 완료 로그를 수집한다. 출력되는 directory와 run_id, 사용한 탭/계정 수,
오류 여부를 전달한다. Token·HAR·Cookie·원본 IP 로그를 채팅에 보낼 필요는 없다.
Cloud 담당이 읽기 전용으로 아래 자료를 보존하고 결과를 해석한다.

```text
/var/lib/yeodam/nginx-read-demand/<run_id>/
  capture.json    # 단계별 경계, site hash
  events.jsonl    # 익명 출처·시각·정규화 경로·상태만 보존
  summary.json   # 단계/출처별 집계와 rate-burst 비교
  peer.key       # 서버 root 전용, 전송·Git 등록 금지
```

30분이 지났거나 세션을 잃었다면 기존 자료는 보존하고 `begin --abandon-current`로
새 측정을 시작한다. 일시적 SSM 종료가 기존 capture의 경계를 자동으로 끝내지는 않는다.
완료 후 새 측정은 `begin`으로 시작할 수 있다.

## 계산과 선정 기준

- 서버 완료 시각 `msec`에서 `request_time`을 빼 요청 시작을 **추정**한다. 둘 다 ms 정밀도다.
  실제 limiter 단계의 시각·TLS accept 시각과 일치한다는 뜻이 아니다.
- GET/HEAD의 정규화된 `/api/` 경로를 합산하고 CSRF·OAuth·health를 제외한다.
  현재 정규화가 `other`로 만드는 미등록 경로·정적 파일은 표본에서 제외된다.
  이 측정은 확인한 정상 FE API의 수요이며 미등록 API/공격 경로의 전체 도착량이 아니다.
- 같은 peer의 모든 경로/단계를 하나의 타임라인으로 합산한다. 실제 FE의 SSR이 하나의
  출처에 모이는지 제어된 화면 진입 시각과 함께 확인한다. peer_scope의 private/public만으로
  SSR·공격·사용자를 확정하지 않으며, 원 사용자 IP를 임의의 Header로 복원하지 않는다.
- 이동1초의 최대 요청 수, 이동10초·30초의 최대 요청률을 함께 표시한다. 고정 UTC 초
  버킷 경계에 걸친 burst를 놓치지 않는다. 짧은 구간은 지속 처리량 검증이 아니다.
- 후보 rate는 관측한10초·30초·1초의 최대율을 각각 올림한 비교 지점이다. **자동 채택값이
  아니다.** 운영 여유와 공유 IP 인원은 관측치와 분리해 나중에 결정한다.
- 각 rate에 대해 `excess=max(0, 이전excess-rate×경과초+1)`을 누적한다. 첫 요청의 excess는0,
  필요한 burst는 최대 excess의 올림이다. 이 burst로 같은 타임라인의 거절 예정0을 확인한다.
  이 계산은 관측 타임라인을 수용할 최소값이고 Nginx의 ms 반올림·실행 단계 차이를 완전히
  복제하지 않는다. 실제 후보는 이후 Nginx Dry Run에서 다시 확인한다.
- rate와 burst에는 여러 조합이 가능하므로 정상 trace만으로 하나의 최적값을 결정하지 않는다.
  측정3회는 모집단 p99가 아니다. 정상 수용 조건과 뒤이은 부하 시험의 보호 효과를 함께 보고 채택한다.

기록된 익명 이벤트로 다른 rate를 비교할 때는 로컬에서 실행한다. 네트워크 요청이 없다.

```bash
python3 tests/load/scripts/nginx-read-demand.py analyze \
  --input '<로컬 events.jsonl>' --rates 2 5 10
```

위 숫자는 사용법 예시이며 선정한 한도가 아니다. 20r/s·burst40은 여전히 보류 상태다.
추가5초 대기와 quiet_tail은 모든 in-flight 요청의 완료를 증명하지 않는다. 오류·누락·중복·
site 변경이 있으면 rate 선정을 진행하지 않고 원인을 먼저 확인한다. 원본 접근 로그는 기존
회전 경로에 남고 익명 export만 별도 보존한다.

## 2026-10-24 정상 브라우저 실측 결과

사용자가 측정을 위임해 합성 계정 하나와 실제 V1 FE 브라우저로 수행했다.
여행12개, 대표 여행의 폴더13개와 한 폴더 사진150장을 새 시험 행으로 구성했다.
기존 완료 기준선의 사진 S3 키만 참조하며 기존 사용자·여행 소유권은 바꾸지 않았다.
새 업로드·AI 처리는 수행하지 않았고, 이 사진 배치는 실제 AI 분류 결과가 아니다.

Run: `58ebbe06-5015-40fb-a893-5f18a827018f`,
UTC `2026-10-23T17:35:22.664Z`~`17:46:37.185Z`.
동일 계정·네트워크의 단일 탭3회와 다중 탭을 측정했다. 각 단일 흐름은
지도→목록/필터/정렬→폴더→150장 목록→원본6장→장소 검색3회다.
다중 탭은 지도·목록·사진을 동시 진입한 뒤 사진 목록3개 동시 진입을 추가했다.
HTTP 캐시를 지우지 않았으며 첫 반복은 수동 조작 간격이 이후 자동 조작보다 길다.

| 단계 | 브라우저 출처 API | 서버 조회 출처 API | 브라우저 출처 최대 이동1초 / 10초 |
| --- | ---: | ---: | ---: |
| single-1 | 28 | 5 | 9 / 9 |
| single-2 | 28 | 5 | 12 / 23 |
| single-3 | 28 | 5 | 12 / 24 |
| multi-tab | 39 | 6 | 26 / 27 |

총144건 모두200, 설정 Hash 유지, 익명 이벤트 중복·필드 누락 검사를 통과했다.
비선정147건은 정적 파일·화면·헬스 등으로 이 API 수요에서 제외했다.
서버 조회 출처21건은 `/api/users/me`18건과 여행 상세3건으로, 제어한 화면 진입과
FE SSR 소스에 부합한다. 출처의 public/private 구분만으로 사용자 수를 추정하지 않는다.
브라우저 출처123건의 최대 이동10초 요청률은2.7RPS다. 사진150장 목록은18장씩9회
조회되며 세 탭이27회를 만든다. 따라서1초 피크26회를 지속 허용률26RPS로 바로 옮기지 않는다.

| 공통 rate 후보 | 브라우저 출처 전체 타임라인 수용에 필요한 최소 burst |
| --- | ---: |
| 3r/s | 23 |
| 4r/s | 22 |
| 5r/s | 21 |
| 10r/s | 16 |
| 20r/s | 9 |

각 최소 burst의 연속 leaky-bucket 재생에서 거절 예정은0건이다. 실제 Nginx 판정이나
공격 방어 성공을 의미하지 않는다. 새 **Dry Run 제안은5r/s·burst30·nodelay**다.
rate5는 실측 지속률2.7뿐 아니라 코드상 검색300ms(약3.33RPS)와 동일 IP3탭의
각2초 Polling(합계1.5RPS)을 함께 허용한다는 정책 가정을 올림한 값이다.
이 검색·Polling 동시 조건은 이번에 실측하지 않았다. burst30은 실측 최소21보다
9건을 더 두는 시험 여유이며 자동으로 도출된 최적값이 아니다.
SSR은 여러 사용자가 한 FE 출처에 합산될 수 있어 동일 한도의 안전성을 이번 계정 하나로
보장하지 않는다. 실제 Dry Run에서 이 출처의 거절 예정과 정상 화면 영향을 따로 확인한다.

이번 Issue에서 요청 제한은 적용하지 않았다. 기존20/40 제안을 최종 채택한 것도 아니다.
다음 단계는 이 후보의 실제 Dry Run과 정상 흐름 확인이며, 이후 통제된 반복 요청·업로드
병행 시험과 강사 재시험에서 유지·조정 여부를 판단한다.

### 시험 데이터 준비와 세션 폐기

계정에 여행이 없으면 Cloud 담당이 V1 App에 두 스크립트를 함께 설치할 수 있다.
실제 기준선 여행 `2048043`(10장), `2048045`(150장)가 필요한 일회성 staging 전용 도구다.
다른 DB에 일반화한 seed 도구가 아니며 실행마다 새 UUID를 사용한다.

```bash
# V1 staging App의 root 전용 도구 디렉터리에서 실행
sudo python3 prepare-nginx-read-fixture.py --run-id '<새 UUID>'
```

동일 디렉터리에 `synthetic-session-pool.py`가 필요하다. Root·Instance ID·쓰기 허용 상태와
완료 기준선을 검사하고 새 합성 사용자·세션과 새 시험 행만 만든다. 세션 파일은
`/run/yeodam-nginx-read-fixture/<UUID>.json`(600), 비밀이 없는 데이터 정보는
`<UUID>.fixture.json`이다. 원본 JWT 비밀키를 내보내지 않는다. 인증 파일 이동은 별도
명시적 허용을 받은 이번 시험에서만 SSM 터널로 수행했고 일반 자동 절차로 추가하지 않았다.

측정 후 해당 파일의 SID와 userId를 대조해 그 세션·CSRF 행만 삭제하고 서버 잔여0을
확인했다. 값·도메인이 일치한 시험 쿠키2개, Mac 임시 인증 파일과 서버 인증 파일도
폐기했다. 시험 여행은 다음 방어 검증용으로 보존한다. **S3 키를 공유하므로 첨부 삭제 API나
S3 삭제로 정리하면 안 된다.** 기존 pool의 사용자 삭제 cleanup도 여행 FK가 있는 이 계정에
그대로 실행하지 않는다. 후속 사용은 새 단기 시험 세션이 필요하다.

원본 익명 `capture.json`·`events.jsonl`·`summary.json`, 후보 재생 결과와 브라우저 동작
기록은 Cloud 로컬 `.local-docs/evidence/416-nginx-read-demand/`에 보존했다.
events SHA256: `fc779ca774e7640f44b7fad65d131fff3124d629399ddcc885b2f744407e3d5c`.
서버 원본 IP와 HMAC 키·인증 파일은 Git에 포함하지 않는다.
