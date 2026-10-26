# V1 여행 생성 제한 Dry Run — #430

> #430 당시의 관측 모드 기록이다. #433 이후의 실제 차단과 최신 수집·원복은
> [생성 실제 차단](NGINX-CREATE-ENFORCEMENT.md)을 따른다.

대상은 서울 Account `483175530259`, V1 App `i-05d320e3a87200d5a`,
`https://v1-staging.yeodam-2gether.com`과 기존 전용 발생기다.
#428에서 확인한 정상 생성 한 여정 1 POST, 실제 3탭의 3 POST와 사진 재시도 추가 생성 0을
출발점으로 **peer당 1r/s·burst5·nodelay** 후보를 관찰한다. 빈 bucket에서 즉시 약6건을
허용하는 여유이며, 6건이 실측 정상 수요이거나 큰 공유 NAT에도 충분하다는 뜻은 아니다.

## 생성만 관측하고 조회는 차단하는 구조

`limit_req_dry_run`은 zone별 옵션이 아닌 context 설정이다.
HTTPS server 전체를 on으로 바꾸면 기존 조회 차단도 풀리므로 다음처럼 분리한다.

- 정확한 `/api/trips` location에서 POST만 내부 named location으로 전달한다.
- named location에 생성 전용 zone, burst5, Dry Run on을 둔다. 외부 418/redirect는 없다.
- GET/HEAD는 기존 서버의 조회5r/s·burst30·실제 차단을 상속한다.
- 사진 `initial-attachments`, 다른 쓰기·인증·HTML은 생성 bucket에 포함하지 않는다.
- 실제 Nginx peer를 사용하며 외부 X-Forwarded-For나 검증하지 않은 JWT를 키로 쓰지 않는다.

named location은 method/URI를 유지하는 공식 내부 전달 방식이다. JSON body·Cookie·CSRF·
request ID·query 보존과 GET 실제429를 로컬 Nginx1.24에서 검증하고 실제 인증 생성·사진도 대조했다.
원래 proxy header·600초 timeout·Keepalive15초·연결 한도는 유지한다.
[limit_req 상속과 Dry Run](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html),
[named error_page](https://nginx.org/en/docs/http/ngx_http_core_module.html#error_page)를 따른다.

## 설치와 원복

Cloud checkout에서 `monitoring/v1-staging/`의 아래4개 파일을 검토해 App의 root 전용
`/opt/yeodam/ops/nginx-create-dry-run/`에 설치한다.

- `nginx_create_dry_run.py`, `install-nginx-create-dry-run.py`
- 의존성 `nginx_read_enforcement.py`, `nginx_read_dry_run.py`

App SSM에서 현재 site를 먼저 확인한다. 예상 SHA가 다르면 변경 내용을 검토한 후 진행한다.
다른 제한·부분 패치·운영 도메인은 자동 적용하지 않는다.

```bash
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
sudo python3 /opt/yeodam/ops/nginx-create-dry-run/install-nginx-create-dry-run.py install \
  --expected-site-sha256 '<검토한 현재 SHA256>'
```

출력은 changed/backup/beforeSha256/afterSha256이다. 백업 후 `nginx -t`·graceful reload하며,
실패하면 원본을 복구한다. 이미 같은 정책은 no-op이다. 컨테이너·DB를 재시작하지 않는다.
#430 적용은 before `070fab16…` → after `4d559ecd…`, backup `apply-qmvtekox`였다.
실제 원복 명령은 이번 시험에서 실행하지 않았다. 필요하면 App SSM에서 다음을 실행한다.

```bash
sudo python3 /opt/yeodam/ops/nginx-create-dry-run/install-nginx-create-dry-run.py rollback \
  --backup /var/backups/yeodam/nginx-create-dry-run/apply-qmvtekox \
  --expected-site-sha256 4d559ecd9dba874dcb6ebd331399b55c63fd101996d0be29d9973433c839b52e
sudo sha256sum /etc/nginx/sites-enabled/yeodam-v1-staging
curl -fsS --max-time 5 https://v1-staging.yeodam-2gether.com/api/actuator/health
```

원래 SHA `070fab168a37a232747a33ba46a3b6282fb998235ecc70adcff4e6ffc8e05a86`와 헬스 UP을 확인한다.
현재 SHA가 바뀌면 자동 원복은 거절한다. 기존 read/keepalive 전용 installer도 다른 정책이 섞이면
안전을 위해 거절하므로, 이전 도구로 새 생성 설정을 덮어쓰지 않는다.

## 2026-10-26 실제 결과

| 시험 | 생성201 | PASSED | REJECTED_DRY_RUN | 정상 조회200 |
| --- | ---: | ---: | ---: | ---: |
| 1RPS·60초 | 61 | 61 | 0 | 30 |
| 같은 IP의 3계정 각1건 | 3 | 3 | 0 | 3 |
| 같은 계정의 3건 | 3 | 3 | 0 | 3 |
| 5RPS·60초 | 301 | 65 | 236 | 31 |

4개 단계 모두 exit0·Dropped0이다. metadata368건은 DB 각1행·region 각1행이었고 미확인 commit·누락·
중복·trip ID 불일치는0이었다. #428의 같은 합성 소유자3명을 사용하고 데이터를 보존했다. 고정200VU,
k6v2.3.0, HTTP/2, 연결 재사용 허용, retry/redirect 없음, 1초 시험 가드는 #428에서 바꾸지 않았다.

5RPS와 겹친 다른 출처의 실제 화면에서는 3탭과 재시도용1탭을 사용했다. 생성4건 모두201/PASSED,
사진4건 모두200/제한 상태 빈 값이고, 여행 모두 COMPLETED·사진 행 각1개였다. 사진1건을 전송 전에
실패시킨 뒤 같은2075492에 재전송했으며 생성 POST는 추가되지 않았다. 완료 후 새로고침도 추가 생성0.
생성 응답 손실·진행 중 새로고침 복원·중간 batch 실패는 검증하지 않았다.

수집 구간은 KST14:11:49~14:18:23이다. App/Loki514건 일치, 누락·중복0이며 394개1초 표본은
sampleErrors0·collectionComplete/siteUnchanged true였다. CloudWatch BE84건·AI276건도 보존했다.
오류 로그240행은 Dry Run236·사진 body 임시 파일 버퍼4로 분류했고 연결 슬롯·FD 부족은 검출0이었다.
App CPU는5RPS 중앙값14.21%·사진 처리와 겹친 최대96.06%였다. CPU 여유나 방어 성공을 증명하지 않는다.

## Grafana와 원본

스테이징 Grafana → Explore → Loki에서 시험 시간을 지정한다. 기존 stream을 유지한다.

```logql
{environment="staging",service_name="v1-nginx",stream="access"}
| json
| method="POST"
| normalized_route="/api/trips"
| limit_req_status="REJECTED_DRY_RUN"
```

HTTP201과 거절 예정 판정을 따로 읽는다. 이번 초과236건도 BE에 전달했고 지연·429를 주지 않았다.
생IP·JWT·Cookie·비밀키는 export/Git에 넣지 않는다. 시험 SID4개·CSRF·비공개 파일·시험 쿠키·사진은 폐기했고,
사용자·여행·이미지·결과는 보존했다. 원본은 Cloud 로컬 `.local-docs/evidence/430-nginx-create-dry-run/`에 있다.

```bash
# Cloud checkout. 반출한 원본만으로 재계산하며, 그래프에만 matplotlib이 필요하다.
python3 tests/load/scripts/analyze-nginx-create-dry-run.py --evidence '<원본 절대 경로>'
python3 tests/load/scripts/render-nginx-create-dry-run.py --evidence '<같은 절대 경로>'
```

그래프는 원본 로그의 사후 재구성이며 실시간 Grafana 캡처가 아니다. 완료 화면은 실제 브라우저 이미지다.
이 표본에서는 정상 생성을 거절 예정으로 분류하지 않았으므로 다음은 같은 후보의 실제 차단과 정상 생성·
사진 보존을 비교한다. 더 큰 공유 NAT·분산 출처·장기 혼합 부하를 보장하지 않으며, 전역 rate·limit_conn·
TCP 방어를 동시에 추가하지 않는다.
