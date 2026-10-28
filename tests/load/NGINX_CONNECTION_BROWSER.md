# 연결 유지·교체 부하 중 독립 브라우저 검증

#447에서는 HTTP/2 생성 요청 부하 중 정상 화면을 확인했다. 이번에는 #438의 연결 유지·교체 시나리오를 같은 브라우저 가드와 연결한다. V1 스테이징의 기존 방어를 유지하며 운영과 V2 QA는 변경하지 않는다. 실제 LTE 경로를 대신하거나 원래 공격을 완전히 재현하는 시험은 아니다.

## 준비 조건

[브라우저 동시 검증](NGINX_BROWSER_UNDER_LOAD.md)의 host·ENA·내부 조회·Mac 외부 HTTP/2 probe·브라우저 네 화면 기준선을 먼저 시작한다. 부하에 인증·사진·AI 작업은 사용하지 않는다. 정상 브라우저만 기존 합성 계정의 새 세션을 사용하고 종료 후 자신의 세션·쿠키·임시 파일을 폐기한다.

브라우저 기준선은 익명 로그인 화면, 인증 후 홈 지도·여행 목록·상세의 기대 UI를 확인한다. 네 화면이 모두 통과하고 checkpoint가 최신이어야 한다. `context.json`과 수집기 UUID는 이번 실행에서 새로 만든다. App·발생기·PLG는 기존 자원을 재사용한다.

## 실행 범위와 중단

Cloud 저장소 루트에서 다음 컨트롤러를 실행한다. 경로는 이번 실행의 private 폴더로 바꾼다. `--execute`를 빼면 계획만 출력하며 AWS 조회나 부하는 실행하지 않는다.

```bash
python3 tests/load/scripts/run-nginx-external-trials.py \
  --context /private/tmp/이번시험/context.json \
  --external /private/tmp/이번시험/external \
  --browser /private/tmp/이번시험/browser-guard \
  --output /private/tmp/이번시험/trials \
  --seconds 60 --execute
```

| 순서 | 부하 | 확인할 차이 |
| --- | --- | --- |
| 1 | 미인증 POST 100 RPS | 연결 재사용 HTTP의 대조 조건 |
| 2 | 미인증 POST 450 RPS | 요청량 증가 중 정상 화면 |
| 3 | TLS 연결 192개 목표, 초당 20개 개방 | 연결 유지와 출처별 동시 연결 정책 |
| 4 | 신규 TLS 연결 초당 120회 시도 | SYN·TCP/TLS 교체 중 정상 새 연결 |
| 5 | POST 450 RPS와 연결 교체 동시 실행 | 같은 발생기 출처에서 정책을 공유하는 혼합 부하 |

60초는 네 화면을 한 번씩 반복할 시간을 확보하기 위해 선택했다. 기존 기본값은 30초이며 이번 실행에서 명시적으로 늘린다. HTTP는 5초 준비·5초 상승·50초 목표 구간이다. 전체 60초가 목표 RPS인 것은 아니다. 192개는 설정 목표이며 출처별 conntrack 128개와 SYN 정책 때문에 실제 개방 수는 낮을 수 있다. 정책을 풀어 목표를 억지로 달성하지 않는다. 단계 사이의 대기는 5초이며 커널 conntrack 상태를 초기화하지 않는다. 앞 단계의 흐름이 다음 단계에 영향을 줄 수 있으므로 순차 혼합 상황의 결과로 해석한다.

외부 probe 또는 브라우저 실패·정체 시 2초 감시 루프에서 발생기 unit 중단을 요청한다. SSM 전달 지연이 있어 2초 내 실제 종료를 보장하지 않는다. 각 단계 전후 host·ENA·내부 정상 조회, 메모리·디스크·CPU 및 Nginx 설정 SHA를 검사한다. 이 자원 검사는 연속 실시간 가드와 구분한다. unit에는 시간·CPU·메모리 상한을 두고, 부모가 두 부하 자식 모두의 종료를 기다린다. 실패한 단계를 자동 재시도하거나 다음 단계로 확대하지 않는다.

## 결과와 자료 대조

SSM 명령의 `Success`는 정상 사용자의 성공률도, 모든 TCP 연결 개방도 의미하지 않는다. 발생기 `tcp.json`의 시도·TLS 개방·최대 소켓·타임아웃·회복 probe와 k6 요청 수·미시작·실패를 별도로 보존한다. 혼합 부하에서 HTTP가 일찍 중단되면 실제 중첩 시간과 달성하지 못한 목표를 그대로 남긴다.

컨트롤러의 단계 라벨은 SSM 제어 시각이다. HTTP 프로세스 시작·종료 파일과 TCP actor별 시각으로 실제 부하 창을 따로 구성한다. 브라우저의 첫 요청부터 캡처까지가 그 창 안에 들어간 완전한 round만 단계별 화면 검증 수에 포함한다. 프로세스 창에는 k6 준비 시간이 포함되며 렌더 지연 측정값으로 사용하지 않는다.

App·PLG·발생기는 아래로 보존한다. 출처 IP 매핑·세션·전송 payload는 반출본에서 제외한다. generator marker 헬스 요청의 requestId와 App sourceId를 대조해 브라우저와 부하 출처가 다른지 확인한다.

```bash
python3 tests/load/scripts/fetch-nginx-rehearsal.py \
  --run-id '<capture UUID>' \
  --ops-directory /opt/yeodam/ops/nginx-edge438 \
  --include-edge-generator --output /absolute/새증빙폴더
```

`browser`, `browser-guard`, `external`, `trials`와 정제된 `generator-source-marker.json`을 같은 증빙 폴더에 모은다. marker 파일에는 `status`와 `requestId`만 넣는다. 분석은 네트워크 없이 원본에서 다시 실행할 수 있다.

```bash
python3 tests/load/scripts/analyze-nginx-connection-browser.py \
  --archive /absolute/증빙폴더 --font /absolute/한국어-font.ttf
```

`analysis/summary.json`과 한국어 그래프를 확인한다. App/Loki 요청 ID 다중집합, 브라우저 응답 ID, 출처 분리, host/ENA/TCP 수집 공백과 종료·폐기 증거를 함께 검토한다. 원래 LTE 접속 실패가 재현되지 않았다면 원인 해결이나 모든 사용자 보호를 주장하지 않는다.
