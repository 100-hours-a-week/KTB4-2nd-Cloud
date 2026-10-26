# V1 스테이징 연결 수용과 TCP 보호 — #436

서울 Account483175530259의 V1 App i-05d320e3a87200d5a와 기존 발생기 i-0c53b65eb35dd2b42만 대상이다.
운영/V2 QA 정책이 아니다. API429와 TCP접수 거절을 구분한다.

## 비교 조건과 판단

Keepalive15초, Worker auto/실제2개, 조회5r/s·burst30과 생성POST1r/s·burst5를 유지한다.
1536연결/Worker·FD4096을 자체 검증 후 스테이징에 채택했다. 같은 출처3계정9클라이언트36조회200, 다른 출처 부하와 실제150장 여정 중첩 및DB/S3감사를 통과했다.
150장 여정556.260초 중 초기60초 이후 약8분17초를 겹쳤고 정상조회224건 실패0/최대361.804ms였다.
custom summary 미생성은 별도 기록하고 setup 시간 검사/fallback을 수정했다. 강사 재시험·운영 적용은 별도다.
슬롯에는client와upstream이 포함되며 사용자 수가 아니다. 슬롯 확대 자체가 공격 방어도 아니다.

10/26 기존768/Worker에서64·512개,1536/Worker에서2048·2560개를 측정했다.
첫2048시도는 발생기soft FD1024에서 약1016개 연결/OSError로 실패했다. 원본을 보존했다.
발생기 시험Unit에만 LimitNOFILE8192를 지정한 재시험은2048개 모두 연결됐다.
2560개·50초는 모두 연결, App CPU 관측 최대35.955%, MemAvailable최소1740248KiB,
Swap입출력/OOM증가0, 정상 API guard 유지였다. 정해진 짧은 패턴이며 안전한 물리 최대치는 아니다.
reload 전후 workerRSS·캐시 상태가 달라 RSS차이를 모두 연결당 비용으로 환산하지 않는다.

출처별 채택값은 kernel conntrack128개, 최초SYN20/s·burst40이다.
64개 단독·신규률 단독·128개 조합과3분 유지/교체 혼합을 구분했다.
3계정9클라이언트·150장 여정까지 반영해 스테이징에 채택했다. 한 IP는 한 사용자가 아니다.
다수 출처 합산 고갈·회선 포화·대규모 NAT까지 보호한다고 주장하지 않는다.

## 역할과 제한

별도 inet yeodam_tcp436 테이블만 관리한다. input priority-10, 목적지80/443의 NEW·최초SYN을 검사한다.
UFW/Docker 규칙을 flush하거나 우회 accept하지 않는다. loopback은 제외한다.
실제 패킷 출처를 사용하고 X-Forwarded-For/JWT를 신뢰하지 않는다. 기존 연결은 강제 종료하지 않는다.

ct count는 커널 추적 흐름이며 ss ESTABLISHED·Nginx Active·HTTP/2 stream과 같지 않다.
종료된 conntrack 행이 남아 있다는 사실만으로 같은 기간 계속 차단한다고 추정하지 않는다.
격리 namespace에서 IPv4/IPv6 각각16개 허용·17번째 차단·2초 뒤 재접속을 확인했다.
공개 IPv6 경로의 검증을 대신하지 않는다. 동적 set 크기는 각각16384로 제한했다.

SYN 재전송도 카운터에 포함될 수 있다. drop은 HTTP429를 보내지 않아 클라이언트 timeout으로 보일 수 있다.
차단 패킷·클라이언트 시도/성공·HTTP 응답 수를 각각 보존한다.

## App 배포·원복

Mac의 Cloud 작업 브랜치에서 다음 소스를 기존 SSM 전송 방식으로 App의
/opt/yeodam/ops/nginx-tcp436/에 함께 전달한다. 공개 포트·Secret 권한은 추가하지 않는다.

- nginx_tcp_guard.py, install-nginx-tcp-guard.py, nginx-tcp-metrics.py
- nginx-rehearsal.py, nginx-connection-observer.py, nginx-create-resources.py
- nginx-tcp-normal-monitor.py, synthetic-session-pool.py

App SSM/root에서 실제 사이트·main·실행 이미지를 읽는다. hash가 다르면 새 값을 무조건 신뢰하지 않는다.

~~~bash
sha256sum /etc/nginx/nginx.conf /etc/nginx/sites-enabled/yeodam-v1-staging
python3 /opt/yeodam/ops/nginx-tcp436/install-nginx-tcp-guard.py status \
  --expected-site-sha256 '<확인한 사이트SHA>'
python3 /opt/yeodam/ops/nginx-tcp436/install-nginx-tcp-guard.py capacity \
  --expected-site-sha256 '<확인한 사이트SHA>' --expected-main-sha256 '<확인한 main SHA>' \
  --connections 1536 --lease-seconds 1200
~~~

정확한Instance·2vCPU·파일hash를 검사하고 nginx -t/reload를 수행한다.
신규 worker의 실제FD4096·활성Worker2개를 확인한다. 이전 worker는 진행 연결을 마치고 종료한다.
원본 main과 전후hash는 /var/lib/yeodam/nginx-tcp436/capacity.json에 남는다.

다음은 조합 차단의 후보다. 관측모드는 --enforce를 빼고, 단독 시험은 --rate 0 또는 --per-source 0이다.

~~~bash
python3 /opt/yeodam/ops/nginx-tcp436/install-nginx-tcp-guard.py policy \
  --expected-site-sha256 '<확인한 사이트SHA>' \
  --per-source 128 --rate 20 --burst 40 --enforce --lease-seconds 1200
~~~

nft 문법 확인 후 자기 테이블만 원자적으로 교체한다. lease는 정상 세션/수집기와 무관한 별도 timer다.
연결만 넣는 초기 비교는 가용 메모리768MiB·CPU85% 지속·정상probe·수집최신성을 중단guard로 사용했다.
150장 혼합에서는 정상 사진 처리의 BE cgroup CPU가 약1.3~2코어를 써 첫 실행이 중단됐다.
이 실패는 남기고, 재시험은 정상 업로드60초를 먼저 관측한 뒤 연결 부하를 겹친다.
정상200/1초·메모리768MiB·OOM증가·수집최신성·Nginx CPU1코어 지속 초과를 분리해 확인한다.
초과 시 공격 클라이언트를 먼저 닫으며 정상 업로드와 증거 수집은 함께 종료하지 않는다.
이 값은 SLO나 물리 한계가 아닌 시험 중단guard다.

~~~bash
python3 /opt/yeodam/ops/nginx-tcp436/install-nginx-tcp-guard.py rollback \
  --expected-site-sha256 '<시험 시작 시 사이트SHA>'
~~~

자기 테이블/service만 제거하고 원본 main을 복구한다.
현재main이 자기 변경 후hash와 다르면 다른 작업을 덮지 않고 실패한다. journal/backup을 대조한다.
사이트는 덮지 않는다. 기존 UFW/Docker 규칙의 카운터를 제외한hash·공개 경로를 재확인한다.

최종 여정까지 통과한 설정만 재부팅 후 유지한다.

~~~bash
python3 /opt/yeodam/ops/nginx-tcp436/install-nginx-tcp-guard.py commit \
  --expected-site-sha256 '<확인한 사이트SHA>' --expected-main-sha256 '<최종 main SHA>'
~~~

자기service와metrics timer를 등록하고 시험lease를 해제한다.
반복commit·service stop/start·자동lease rollback을 실제로 확인한 뒤 기록한다.

## 독립 수집과 정상 사용자

상세 수집은 NGINX-FINAL-REHEARSAL 절차를 따른다. #436은
host/process/HTTPS/stub/log를 분리해 stub250ms실패가 호스트·로그 복사를 건너뛰지 않는다.
componentErrorCounts·hostComponentsComplete·stubComplete를 구분한다.
미수집은0이 아니며 collectionComplete=false도 보존한다.
정상 세션 실패로 수집을 종료시키는 외부 waiting controller는 사용하지 않는다.
수집 기본1800초/명시적 최대14400초, CPU20%·Memory128MiB·원본512MiB·root80% 상한이다.

App SSM/root의 독립 정상 감독기는 고정 합성owner11007만 사용한다.
실행 BE의 JWT 설정을 App 메모리에서 읽고 키/토큰을 stdout·argv·파일로 내보내지 않는다.
30분 세션을20분마다 교체하고 자기 세션만 폐기한다.

~~~bash
systemd-run --quiet --collect --unit='yeodam-normal436-<새UUID>' \
  --property=RuntimeMaxSec=14430 --property=MemoryMax=128M --property=CPUQuota=20% \
  python3 /opt/yeodam/ops/nginx-tcp436/nginx-tcp-normal-monitor.py \
  --run-id '<같은 새UUID>' --seconds 14400
~~~

/var/lib/yeodam/nginx-connections-426/<UUID>/checkpoint.json의 최신성·failureCount를 확인한다.
실패·회복을 계속 기록하고 수집기를 종료시키지 않는다. 부하만 중단하며 이미 수락한 업로드·수집은 유지한다.
시작을 모르면 미리 켜고 마감시각을 명시한다. 상한 전 새 수집의 첫 표본을 확인한 뒤 이전 수집을 종료한다.

기존 발생기의 nginx-tcp-capacity.py는 plan을 먼저 확인한다.
실행은 정확한target·연결수·속도·시간·새output·--execute를 요구한다.
시험Unit에만 LimitNOFILE8192와 RuntimeMaxSec=시험시간+15를 지정한다.
hold는TLS/HTTP1.1 재사용, tcp-hold는TLS미시작, churn은연결교체다.
공격과 정상 발생기를 분리할 때는 명시적 --local-driver로 macOS의 무인증 클라이언트를 쓸 수 있다.
이 모드는 정확한 스테이징 도메인만,256연결·80시도/s·900초 이내로 제한한다.
다른 운영 대상과 자유로운 IP 입력은 허용하지 않으며 시험 계정은 기존 발생기에만 둔다.
주요옵션은 --mode, --connections, --rate, --seconds,
--confirm-target https://v1-staging.yeodam-2gether.com, --output, --execute다.
회복probe가 다른 부하와 겹친 시각도 대조한다.

같은NAT는 nginx-tcp-normal.py의3계정9API클라이언트·36조회로 확인한다.
세션 전송은 승인 범위의 SSM 암호화터널·비공개 파일만 사용하고 종료 후 폐기한다.
150장 여정은 upload-baseline·고정Manifest·COMPLETED/DB150/S3원본150·크기로 대조한다.
공격과 정상 업로드의 출처를 구분한다. 같은IP의 초과 부하/정상 부하를 다른 출처 보호로 해석하지 않는다.
짧은 중첩·회복 후 성공을 전체 부하 중 여정 성공으로 확대하지 않는다.

## 관측과 사후 증거

[한국어 연결 보호 검증](https://grafana-staging.yeodam-2gether.com/d/yeodam-v1-tcp-defense)
대시보드에서 연결/설정슬롯·차단패킷·적용/수집최신성·CPU·가용메모리·설정값을 본다.
기존 V1 source의 요청·5xx·로그도 같은 절대시간으로 확인한다.
기존 Alloy textfile로15초마다 수집하므로 보존기간 내 사후 조회할 수 있다.
비활성·테이블 조회 실패·표본 결측을 구분한다.

원본은 runUUID·UTC/KST 시작/종료·설정hash·발생기FD/자원·실패·호스트·정상request ID로 연결한다.
App/Loki ID대조는 도달하지 않은 요청의 성공을 증명하지 않는다.
한국어PNG는 단일2160×810·KST로 만들고 실시간 화면과 원본 재구성을 구분한다.
결과/최종채택은 Cloud .local-docs/436-nginx-tcp-guard.md에 남긴다.

## 강사 시험 직전

App SSM/root에서 검토한 실제 사이트SHA로 아래를 실행한다. UUID 두 개는 서로 다른 새 값이다.
~~~bash
python3 /opt/yeodam/ops/nginx-tcp436/nginx-rehearsal.py start \
  --run-id '<수집UUID>' --seconds 14400 --detailed-processes \
  --expected-site-sha256 '<실제사이트SHA>'
systemd-run --quiet --collect --unit='yeodam-tcp436-normal-<정상UUID>' \
  --property=RuntimeMaxSec=14430 --property=MemoryMax=128M --property=CPUQuota=20% \
  python3 /opt/yeodam/ops/nginx-tcp436/nginx-tcp-normal-monitor.py \
  --run-id '<정상UUID>' --seconds 14400
~~~

수집은 정상 세션 갱신과 별도 Unit이다. 두 checkpoint의 시각과 실패 수를 따로 확인한다.
정상 UUID를 수집 plan.json의 normalRunIds 배열에 추가해야 사후 아카이브에 함께 들어간다.
정상 세션 갱신이 실패해도 수집기를 STOP하지 않는다.4시간이 다 되면 새 UUID로 겹쳐 시작한다.
이는 장시간 무제한 수집이 아니라 상한을 명시한 실행이며 원본512MiB/root80% 상한도 남는다.

시작/종료 시각은 UTC와KST로 기록한다. 종료 뒤 정상 Unit을 stop해 자체 SID 폐기를 확인하고,
수집 stop 후 result.json·원본로그 unreadBytes·host/stub 오류를 확인한다.
fetch-nginx-rehearsal.py의 --ops-directory /opt/yeodam/ops/nginx-tcp436으로 내려받으면
정상 UUID 자료·V1 source/TCP 대시보드 시계열·비식별 App/Loki·CloudWatch가 보존된다.
외부 발생기 시도/성공/timeout과 정상 실제 여정은 별도로 받아야 사용자 손실을 판단할 수 있다.
