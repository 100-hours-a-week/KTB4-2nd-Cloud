# 여담 Cloud

여담은 휴대폰과 디지털카메라에 흩어진 여행 사진을 모아, 촬영 시각과 위치를 바탕으로 앨범과 지도에 정리하는 서비스입니다.

이 저장소에서는 AWS 인프라, 배포 파이프라인과 운영 도구를 관리합니다. V2는 ECS 기반으로 운영하고 있으며, V3 Kubernetes 전환 작업을 진행하고 있습니다.

[서비스](https://yeodam-2gether.com) | [Cloud Wiki](https://github.com/100-hours-a-week/KTB4-2nd-Cloud/wiki) | [팀 설계 문서](https://github.com/100-hours-a-week/KTB4-2nd-wiki/wiki)

## 구성

FE와 BE, AI API를 ECS에 배포하고 ALB로 연결합니다. MySQL에는 서비스 데이터를, App Redis에는 인증 상태를 저장합니다. 무거운 AI 작업은 GPU EC2와 외부 RunPod에서 처리하며, 작업 상태를 관리하는 AI Redis는 App Redis와 분리했습니다.

Kafka와 Debezium으로 Outbox 이벤트를 전달하고, Prometheus, Loki, Grafana와 CloudWatch로 서비스와 호스트 상태를 확인합니다. 인프라는 Terraform으로 관리합니다.

## 주요 작업

### DB 이전과 요청 처리

여행 데이터 100만 행 이상을 준비하고 실제 API 요청이 들어오는 동안 Snapshot부터 DB 전환까지 시험했습니다. 인증 변경은 Debezium으로 Redis에 전달하고, 전환 중 새 요청은 별도 Kafka 경로로 수락했습니다.

이후 조회 인덱스와 CDC 처리, 묶음 커밋을 개선했습니다. 동일한 50 RPS 시험에서 요청 생성부터 DB 처리 완료 기록까지의 p95는 **52.607초에서 18.849초**로 줄었습니다. 운영 원본 DB 이전 실적이 아닌 스테이징 리허설 결과입니다.

[실험 기록](https://github.com/100-hours-a-week/KTB4-2nd-Cloud/wiki/Dev-DB-무중단-마이그레이션) | [묶음 커밋 비교 PR](https://github.com/100-hours-a-week/KTB4-2nd-Cloud/pull/473)

### 배포와 관측

각 서비스가 발행한 이미지를 Digest로 고정하고, Cloud PR에서 이미지 조합을 검증합니다. 검증한 조합을 스테이징에 배포한 뒤 같은 조합을 운영으로 승격합니다.

운영 중에는 서비스 지표와 호스트 로그를 같은 시각으로 대조합니다. Nginx 연결 고갈을 분석하고, 정상 사용자에게 미치는 영향을 확인하며 방어 설정을 보완했습니다.

[CI 검증 범위](deploy/cloud-pr-gate.md) | [V2 배포 Workflow](.github/workflows/v2-production-release.yml) | [Nginx 장애 기록](https://github.com/100-hours-a-week/KTB4-2nd-Cloud/wiki/Incident-2026-09-29-Nginx-연결-고갈)

### 단일 및 다중 Task 부하 비교

같은 BE 이미지와 DB 조건에서 Task 1개와 2개를 비교했습니다. 목록 조회 80%, 여행 메타데이터 생성 20% 조건으로 각각 **250 RPS와 550 RPS를 3분간 유지**했습니다. 장시간 운영 용량이나 서비스 전체의 처리량을 의미하지는 않습니다.

[시험 코드와 검증 PR](https://github.com/100-hours-a-week/KTB4-2nd-Cloud/pull/475)

## 저장소 안내

| 경로 | 내용 |
| --- | --- |
| `terraform/` | AWS 인프라 구성 |
| `deploy/` | 배포 설정, 이미지 Manifest와 운영 안내 |
| `monitoring/` | 지표와 로그 수집, 대시보드와 알림 |
| `scripts/` | 배포와 점검, 이전 도구 |
| `tests/ci/` | 설정 검증과 격리된 이미지 통합 시험 |
| `tests/load/` | API 부하 시나리오와 결과 대조 도구 |
| `nginx/`, `app-compose.yml`, `worker-compose.yml` | V1 구성과 운영 이력 |

이전 시험과 V1 도구도 이력으로 남아 있습니다. 현재 운영 대상으로 실행하기 전에 사용 환경과 필요한 설정을 확인해야 합니다.

## 변경과 배포

작업 브랜치에서 PR을 만들고 필수 `cloud-ci-gate`를 통과한 뒤 main에 병합합니다. V2 이미지 Manifest 변경은 스테이징 배포로 이어지며, 운영 배포는 성공한 스테이징 Run을 지정해 실행합니다.
