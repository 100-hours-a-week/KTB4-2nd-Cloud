# 공유 GPU 루트 디스크 확장

GPU는 staging Terraform State가 소유하며 운영에서도 같은 호스트를 사용한다. #340은 `aws_instance.ai_gpu.root_block_device.volume_size`만100→200GiB로 바꾼다. 데이터 EBS20GiB, 이미지·모델 캐시와 실행 컨테이너는 유지한다. 온라인 확장 절차이며 Docker 재시작이나 EC2 재부팅을 포함하지 않는다.

## 대상과 변경 전 확인

| 항목 | 검토한 값 |
| --- | --- |
| Account / Region | 483175530259 / ap-northeast-2 |
| GPU Instance | i-05e1ad9dd96dba4ae |
| 루트 EBS | vol-0fccde58d4fa3e8c8, gp3, 암호화,100→200GiB |
| 루트 파일시스템 | ext4, 확인 당시 /dev/nvme0n1p1 |
| 별도 Qdrant EBS | vol-05d2497eaeabb57d3,20GiB, 변경하지 않음 |

먼저 AWS의 Instance·Root Device·Volume Attachment를 확인하고 루트 Snapshot을 생성한다. 실행 중 Snapshot은 호스트 전체의 애플리케이션 일관성 백업을 보장하지 않으며 별도 Qdrant 데이터 EBS를 포함하지 않는다. Snapshot은 요청 시점 데이터를 비동기로 복사하므로 pending 중에도 원본 볼륨을 계속 사용할 수 있다. 생성 요청과 정확한 Volume ID·pending/completed 상태를 확인한 뒤 확장을 진행하고, 복사 완료는 별도로 확인한다. pending을 완료된 백업으로 기록하지 않는다. EBS 크기는 직접 줄일 수 없다.

Cloud 저장소 루트에서 `terraform/v2/staging`의 기존 backend로 init한 뒤 fmt/validate/plan 순서로 확인한다. State/Plan 원본은 비공개 임시 디렉터리에만 저장한다. 기존 부하 발생기를 유지하는 `-var=enable_load_generator=true`를 포함하고 현재 환경의 다른 입력도 확인한다. 전체 Plan에서 이 Issue 밖의 변경을 함께 적용하지 않는다. 다른 채팅의 미병합 State 변경이 있으면 전체 Plan을 보존하고 GPU만 지정한 Plan도 검토한다. 적용 가능한 액션은 GPU의 in-place 루트 크기100→200 한 건이며 EC2 교체·Stop/Start·다른 필드 변경이면 중단한다.

## 파일시스템 반영

AWS 볼륨 변경이 `optimizing` 또는 `completed`이고 Size200GiB인 것을 확인한다. GPU의 SSM 세션에 접속한 뒤 검토한 `scripts/expand-shared-gpu-root.sh`를 전달해 root로 실행한다. AWS 볼륨 크기만 늘리면 기존 ext4가 자동으로 전체 크기를 사용한다고 가정하지 않는다.

```bash
# GPU 호스트: 전달받은 검토 스크립트 경로 사용
sudo bash /path/to/expand-shared-gpu-root.sh
sudo bash /path/to/expand-shared-gpu-root.sh --apply
```

첫 명령은 호스트·루트 파티션·EBS Serial·200GiB·도구를 검사하고 종료한다. 두 번째는 같은 검사를 반복한 뒤 파티션1과 ext4를 늘린다. `growpart` 임시 파일은 /run(tmpfs)에 두어 가득 찬 루트의 임시 파일 부족을 피한다. 파일시스템을 포맷하거나 Qdrant EBS를 확장하는 도구가 아니다.

잘못된 대상·미반영 EBS 크기·지원하지 않는 파일시스템이면 중단한다. `growpart`가 이미 확장된 파티션의 `NOCHANGE`를 반환한 경우에만 ext4 확장으로 진행한다. 그 밖의 실패는 출력 보존 후 중단하고 다른 디바이스로 바꿔 재시도하지 않는다. `resize2fs` 오류는 원본 메시지와 `findmnt`/`lsblk`를 확인하며 mkfs·데이터 삭제·재부팅으로 해결하지 않는다.

## 완료 판정

`df -hT / /data/qdrant`에서 루트 파일시스템 증가·여유 확보와 별도 데이터 볼륨 유지, AWS에서 동일 Instance/Volume ID·Size200을 확인한다. boot ID·각 컨테이너 ID/기동 시각/재시작 수·설정 해시를 변경 전과 대조하고 기존 임베딩8000 및 Qdrant6333/6335 readiness를 조회한다. 후속 Plan에서 GPU 변경이 남지 않았는지 확인한다. 이 결과는 디스크 확장과 기존 실행 보존이며 동시 AI 부하 성능 검증은 아니다.
