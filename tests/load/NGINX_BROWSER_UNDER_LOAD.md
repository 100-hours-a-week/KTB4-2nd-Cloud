# 자체 부하와 독립 브라우저 동시 검증

#442의 생성 API·HTTP/2 부하와 #445의 실제 화면·TCP 관측을 같은 시간에 실행한다. V1 스테이징만 대상으로 하며 기존 방어·운영·V2 QA를 변경하지 않는다. 원래 공격의 본문·인증·Stream 수를 모르므로 완전 동일 재현이라고 부르지 않는다.

## 준비와 시작 조건

[HTTP/2 재부하](NGINX_HTTP2_REPLAY.md)와 [브라우저 수집](NGINX_EXTERNAL_BROWSER.md)의 기존 App·발생기·PLG를 재사용한다. AWS 계정·인스턴스·사이트 SHA·방어·디스크 여유와 k6 버전을 먼저 확인한다. 부하 합성 사용자3명과 정상 조회 계정11007을 분리하고 자신의 세션만 발급·전송·폐기한다. 실제 사용자 세션과 JWT 서명키는 반출하지 않는다.

호스트·ENA·내부 정상 조회를 새 UUID로 시작하고 `plan.normalRunIds`를 연결한다. 브라우저의 서버 요청 ID로 App의 peer_addr를 확인한 후 그 외부 출처에 대해서만 TCP 관측을 시작한다. 브라우저와 부하의 출처가 실제로 다른지는 사후 App 요청 ID의 sourceId로도 대조한다. 상시 PLG는 유지한다.

브라우저는 한 TaskSpace에서 같은 Page를 사용한다. 무부하 상태의 익명 로그인 화면·인증 후 홈 지도·여행 목록·대표 여행 상세를 먼저 확인한다. 기대 UI의 selector는 실제 snapshot에서 확인한다. 각 이동은 `begin → goto/화면 대기 → saveRound`를 같은 invocation에서 수행한다. 페이지 cache를 우회해 요청을 관측하되 새 TCP/TLS 연결을 강제했다고 표현하지 않는다. 새 연결은 별도 외부 curl로 검증하고 브라우저는 `connectionReused`로 실제 상태를 구분한다.

## 브라우저 가드 연결

`lib/browser-journey-guard.mjs`의 `checkpoint`에 정제한 report, view(login/home/list/detail), captureId와 UI 대기 오류를 전달한다. origin과 UUID가 같은 private 출력 폴더에 checkpoint·history를 저장한다. 네 화면 모두 통과해야 ready가 되고, 최초 실패는 GUARD에 고정해 후속 성공으로 지우지 않는다.

정상 사용자의 HTTP4xx/5xx·취소되지 않은 전송 실패·미완료 API·기대 UI 실패는 중단 대상이다. 폰트 pending·취소된 prefetch는 구분해 보존하고, Console만으로 서버 장애를 확정하지 않는다. API200도 기대 UI 확인을 대신하지 않는다. 라우트의 직접 접속·렌더링을 확인하는 시험이며 OAuth·업로드·모든 카드/버튼 상호작용 성공을 뜻하지 않는다.

로컬 Cloud 저장소에서 기존 컨트롤러에 `--browser`를 추가한다. 아래 모든 경로는 이번 실행에서 만든 새 private 폴더여야 한다.

```bash
python3 tests/load/scripts/run-nginx-h2-trials.py \
  --context /private/tmp/이번시험/context.json \
  --external /private/tmp/이번시험/external \
  --browser /private/tmp/이번시험/browser-guard \
  --output /private/tmp/이번시험/trials \
  --stages smoke1 small16 medium64 instructor128 --execute
```

browser checkpoint가60초보다 오래되거나 다른 capture·실패·기준선 미완료면 시작하지 않는다. 실행 중에는 기존2초 감시 루프가 외부 probe와 browser를 함께 확인하고 SSM으로 해당 발생기 unit의 중단을 요청한다. SSM 전달 지연이 있으므로 실패2초 이내 실제 종료를 보장한다고 쓰지 않는다. 브라우저만 유지되고 관측이 멈추는 경우도 fail closed다. `--browser` 생략 시 기존 #442 동작을 유지한다.

16클라이언트64RPS·30초, 64클라이언트256RPS·30초를 통과한 뒤128클라이언트로299초의 기존133,875개 서버 완료 분포를 송신 스케줄로 근사한다. VU는 실제TCP연결 수가 아니며 요청 ID로 확인한다. 공격 출처에서429나연결 차단이 생긴 사실과 독립 정상 사용자의 실패를 구분한다. 실패 POST를 재시도하지 않고 실제 미시작·실패·DB커밋을 기록한다. 단계가 실패하면 자동 확대하지 않는다.

부하 전·중·후 화면을 같은 조건으로 반복하고 실제 UTC와 예상 UI, status, protocol, Console·transport 오류를 저장한다. 페이지 이동이 실패해 chrome-error/about:blank에 남더라도 시도한 주소가 정확한 V1 스테이징일 때만 해당 오류 화면을 저장할 수 있다. 다른 사이트로의 이동은 내보내기를 거절한다. UI 실패 시 증거를 보존하고 가드를 남긴 뒤 부하 중단부터 확인한다.

## 종료·대조·재현

컨트롤러 종료 후 회복 화면을 확인한다. 브라우저 자체가 실패했으면 자동 재시도로 첫 실패를 덮지 말고 해당 화면·Network·TCP·ENA·서버 로그를 먼저 보존한다. 캡처 timestamp에는 저장 오버헤드가 있으므로 화면 렌더 지연 SLO로 사용하지 않는다. active-stage의 SSM 요청 시각과 실제 k6 송신 구간도 구분한다.

자신의 부하 세션·정상 브라우저 세션과 CSRF를 폐기하고 `/run` 및 Mac private 파일·정확히 일치하는 시험 쿠키만 제거한다. host·ENA·normal·TCP와외부 probe를 마감하고 summary·cleanup 오류·서비스 상태·설정SHA를 확인한다. 시험 계정·여행·지역 데이터는 감사용으로 유지한다.

`fetch-nginx-rehearsal.py --include-h2-generator --ops-directory /opt/yeodam/ops/nginx-edge438`로 App·PLG·발생기와 CloudWatch를 보존한다. browser·browser-guard·external·controller 자료도 함께 모으되 private 세션과 출처IP 매핑은 제외한다. App/Loki 다중집합, 브라우저 요청 ID, 부하201의 DB행을 대조한다.

원본에서 집계와 한국어 그래프를 다시 만드는 명령이다. `matplotlib`이 있는 로컬 Python을 사용하고 한국어 font를 지정한다. AWS 접속은 필요하지 않다.

```bash
python3 tests/load/scripts/analyze-nginx-browser-load.py \
  --archive /absolute/이번시험증빙 \
  --font /absolute/한국어-font.ttf
```

`analysis/summary.json`의 실제 요청 구간별 화면 종류·실패·미시작 요청·출처 분리·자료 누락을 확인한다. LTE와 Mac의 경로 차이, 실제 OAuth·업로드·AI 미검증, 초기 수집 실패는 그대로 남긴다. 재현되지 않았다면 원인을 확정하거나 새로운 방어의 효과를 주장하지 않는다.
