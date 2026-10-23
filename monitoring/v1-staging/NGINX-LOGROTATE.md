# V1 스테이징 Nginx JSON 로그 회전

적용 대상은 V1 스테이징 App `i-05d320e3a87200d5a`다. Ubuntu 기본
`/var/log/nginx/*.log` 규칙은 `.json`을 포함하지 않으므로 독립 규칙을 추가한다.
기존 `.log` 규칙과 Alloy의 수집 경로는 유지한다.

- 기존 `logrotate.timer`로 매일 실행, 비어 있지 않은 로그를 회전한다.
- 회전 파일은 최대 14개를 보관한다. 달력 기준 정확히 14일 보장은 아니다.
- `rename → 새 파일 생성 → nginx rotate(USR1)`로 로그를 재연다. `copytruncate`는 사용하지 않는다.
- `delaycompress`로 직전 회전 파일은 압축하지 않고 다음 회전 때 압축한다.
- 현재 파일과 `.1`은 압축되지 않는다. 최초 약1.06GB 로그도 `.1`로 보존하므로
  첫 회전 자체로 디스크가 줄어들지는 않는다.
- 일일 회전은 파일 크기나 공격 중 디스크 사용량의 상한이 아니다.
  부하 시험에서는 디스크 사용률 80% 중단 기준을 계속 적용한다.

## 적용

저장소의 이 디렉터리 두 파일을 SSM을 통해 App의 동일 디렉터리로 전달한다.
다음 명령은 **App EC2에서** 실행한다.

```bash
sudo bash /opt/yeodam/ops/nginx-logrotate/install-nginx-logrotate.sh install
sudo logrotate --debug /etc/logrotate.conf
systemctl is-active logrotate.timer
```

설치기는 인스턴스 ID를 확인하고 이전 규칙을 `/var/backups/yeodam/nginx-json-logrotate/`에
보관한다. 기본 규칙과의 중복 여부를 전체 설정 debug 검사로 확인한다.
설치 과정은 Nginx 재시작·reload나 강제 회전을 하지 않는다.

## 회전 확인

Nginx와 Alloy가 정상인 때, 기존 로그가 이미 Loki까지 수집됐는지 확인하고 실행한다.
다음 명령은 **JSON 규칙만** 강제 회전한다. 기본 상태 파일 잠금을 사용하므로
다른 logrotate가 실행 중이면 잠금 오류를 확인한 뒤 나중에 실행한다.

```bash
sudo nginx -t
sudo logrotate --verbose --force /etc/logrotate.d/yeodam-nginx-json
sudo stat /var/log/nginx/yeodam-access.json /var/log/nginx/yeodam-access.json.1
```

전후 헬스 요청의 응답 `X-Request-ID`를 기록하고 `.1`과 새 파일, Loki에서 대조한다.
새 파일 inode가 바뀌고 후속 요청이 새 파일에 기록되는지, 기존 Nginx PID가 유지되는지
확인한다. 소량 확인 결과를 고부하에서의 무손실 보장으로 확대하지 않는다.
압축은 다음 회전에서 실행되므로 최초 회전 검증과 구분한다.

새 파일이 기록되지 않으면 Nginx rotate 명령 결과, 파일 권한, Nginx 오류 로그와
열린 파일을 확인한다. 로컬에만 있고 Loki에서 안 보이면 Alloy 상태와 수집 지연을
확인한다. `.1`을 수집 대상에 추가하거나 위치 파일을 초기화해 전체 로그를 재전송하지 않는다.

## 원복

설치기가 출력한 **실제 백업 경로**를 사용한다.

```bash
sudo bash /opt/yeodam/ops/nginx-logrotate/install-nginx-logrotate.sh rollback BACKUP_DIRECTORY
```

설정만 복구한다. 회전 파일·로그 내용·logrotate 상태 파일은 삭제하지 않는다.
Nginx 재시작은 필요하지 않다.

## 근거

- Nginx 로그 재열기: https://nginx.org/en/docs/control.html#logs
- logrotate 유지보수자 매뉴얼: https://github.com/logrotate/logrotate/blob/main/logrotate.8.in
- Alloy 파일 수집: https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.file/
