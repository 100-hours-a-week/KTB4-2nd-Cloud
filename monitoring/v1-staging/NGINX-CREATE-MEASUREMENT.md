# V1 여행 생성의 정상 호출과 과부하 경계 측정

#428은 기존 조회 제한과 client Keepalive15초를 유지하며 여행 생성 호출을 측정한다. 생성 rate/concurrency 제한을 적용하는 작업은 별도다. 대상은 `https://v1-staging.yeodam-2gether.com`, 기존 App과 전용 발생기뿐이다.

## 비교 조건

- 실제 FE에서 합성 계정과 승인된 시험 사진1장으로 생성부터 사진 전송·완료까지 확인한다. `POST /api/trips`와 `initial-attachments`를 구분한다. 실제 화면 표본과 k6 API 재생 표본을 합쳐 정상 수요라고 하지 않는다.
- `nginx-trip-create.js`: metadata만 생성한다. 사진/AI 경로, 자동 재시도, 리다이렉트가 없다. 날짜와 지역 코드1개를 고정하고 UUID hash+iteration으로10자 여행명을 만든다. 신규 합성 사용자3명과 별도 정상 조회fixture를 사용한다.
- 동일 계정3건/3계정각1건 burst는 같은 출처의 동시 API 호출이다. 실제 브라우저3탭/서로 다른 브라우저3개를 자동으로 증명하지 않는다.
- rate는1→5→10→20→30RPS, 각60초부터 진행한다. 경계가 없고 자원 여유가 확인되면 허용한50/75/100으로 확대할 수 있다. 단계별 관측 뒤 진행하며 실패 단계 이후 자동 확대하지 않는다.
- k6v2.3.0, HTTP/2응답 확인, connection reuse 허용, 모든 rate 단계200VUs 고정. VU간 연결은 공유하지 않으므로 같은 TCP 하나의 stream 시험으로 해석하지 않는다. 실제 연결 수는 Nginx로그와1초 상태로 확인한다.
- 정상 조회는 별도 사용자로 지도/목록/상세를2초에1회 교대한다. 같은 발생기 출처이므로 공격 출처와 분리된 사용자 경로 보호를 증명하지 않는다.

첫 비201/정상조회 비200, 생성/정상조회 총지연1초 초과, Dropped 발생에서 단계를 중단한다.1초는 보수적인 시험 중단값이며 서비스SLO/운영 제한값이 아니다. 첫 실패 직전에 이미 접수한 요청은 남을 수 있어 시작/응답/DB행을 대조한다. 강제 재시도로 덮거나 시험 데이터를 지우지 않는다. `dropped_iterations=0`만으로 HTTP성공/무손실을 주장하지 않는다.

## 실행 파일과 순서

로컬에서 기존 세션·관측 도구와 이 이슈의 파일을 검토하고, 각 서버의 전용 경로에만 설치한다. App은 `/opt/yeodam/ops/nginx-create-428`, 발생기는 `/opt/yeodam-load/nginx-create-428`다. 실행 전 Account/Region/Instance, 이미지, siteSHA, DB쓰기 가능 여부, 디스크, 다른 부하, 세션TTL을 확인한다. Nginx reload/restart는 필요 없다.

1. App root에서 `nginx-create-sessions.py issue --run-id <POOL_UUID>`로 새 사용자3명을 만든다. 사용자/여행 데이터를 지우는 기존 generic cleanup은 사용하지 않는다. 읽기fixture는 `nginx-fixture-session.py issue --run-id <READ_UUID>`로 별도 발급한다.
2. private JSON은 기존 `private-pool-transfer.py`의 loopback one-shot서버와 SSM터널로만 전달한다. 발생기 `/run/yeodam-nginx-create-428/{create,read}.json`은600, 디렉터리는700이다. 토큰을 인자/SSM출력/로그에 넣지 않는다.
3. App root에서 `nginx-rehearsal.py start --run-id <CAPTURE_UUID> --seconds 1800 --expected-site-sha256 <검증한SHA> --detailed-processes`를 실행한다. `status`로 표본 증가와sampleErrors0을 확인한 뒤 부하를 시작한다.
4. 발생기 root에서 `scripts/run-nginx-create-stage.py --run-id <STAGE_UUID> --mode rate --rate 1 --seconds 60 --session-count 3`를 실행한다. 단계당UUID는 새 값이며 자동 재실행하지 않는다. 다음 단계 전에 summary·App FD/CPU/오류·헬스를 확인한다.
5. 중단하려면 해당 결과 폴더에 `STOP` 파일을 만든다. wrapper는SIGINT를 보내며 별도deadline도 둔다. k6단계 중단과 App수집 중단은 별개다. 수집 종료는 `nginx-rehearsal.py stop --run-id <CAPTURE_UUID>` 후 최종result를 확인한다.
6. 만료 전 다음 단계를 마칠 수 없으면 생성pool은 `renew`로 같은3명에 새 세션을 발급하고 기존SID를 폐기한다. 읽기fixture는 자신이 발급한 세션을revoke한 뒤 새UUID로issue한다. 파일 전달 후TTL을 다시 확인한다.
7. 종료 시 생성pool `revoke`, 읽기fixture `revoke`로 자신이 발급한SID/CSRF만 폐기하고 양쪽 private JSON과 승인된 Mac사진을 삭제한다. 사용자·여행·원본은 보존한다.

## 수집과 해석

`--detailed-processes`는 기존#426수집에 선택적으로 추가된다. `/proc`에서 Nginx/Java/mysqld/Alloy의CPU tick·FD·누적I/O를 읽고5회마다 기존MySQL status를 조회한다. 명령 인자/환경변수/SQL본문은 수집하지 않는다. 프로세스CPU100%는 논리CPU1개 사용이며 호스트CPU비율과 단위가 다르다. PID가 재사용됐거나 첫 표본이면0이 아닌null이다. DB쿼리timeout은 관측 실패로 남긴다.

발생기는 .5초 간격의 k6CPU tick/RSS와`time --verbose`를 보존한다. wall time과CPU사용시간을 구분한다. App원본로그는 기존#426archive로peer를HMAC화한 뒤 export한다. 원본JSON/JSONL/checksum과분석 코드는 로컬기술문서의evidence에 보존한다.

DB대조는 합성계정 소유·단계별여행명·tripId를 모두 확인한다. started/최종응답/NginxrequestID/DB행의 차이를 보고, 응답을 잃었으나commit된 요청을 일반 실패와 분리한다. 생성만 한 여행의PROCESSING은 이번metadata시험 특성이며 AI완료 증거가 아니다.

현재 로그의 `upstream_response_time`은 upstream전체시간이며 SQL시간이 아니다. upstream연결/첫헤더시간·JDBCpool대기·SQL실행시간은 현재수집만으로 분리하지 못한다. 결과에 이 한계를 남기고 추가 profiling을 임의로 켜지 않는다.

실제 측정값·실행UUID·환경·후보근거와 미검증범위는 Cloud `.local-docs/428-nginx-create-measurement.md`와 연결된 원본근거에 기록한다. 측정 결과를 곧바로 전역 처리량 보장이나 생성 제한값으로 사용하지 않는다.

## 중단 결과를 수집기 성공과 구분

SSM 명령 완료와 k6 성공은 다르다. wrapper는 결과 파일의 `exitCode`를 반드시 확인하고 최종 코드에서는 이 값을 자신의 종료 상태로 전달한다. `exec.test.abort` 시 일부 metric sample은 summary에 반영되기 전에 종료될 수 있다. console event와DB행도 대조하며, status0/최종event없음/서버499를 201확인으로 합산하지 않는다.75RPS는 metric threshold가 녹색이어도 exit108·가드초과로 실패한 단계다.
