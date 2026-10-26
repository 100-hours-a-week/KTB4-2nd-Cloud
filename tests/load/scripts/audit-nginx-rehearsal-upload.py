#!/usr/bin/env python3
"""Read-only DB/S3 audit for the exact synthetic150-photo upload owner on V1 staging."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--session-run-id', required=True)
    p.add_argument('--trip-id', required=True, type=int)
    p.add_argument('--manifest', type=Path, required=True)
    a = p.parse_args()
    UUID(a.run_id);UUID(a.session_run_id)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        p.error('Exact staging App root only')
    os.umask(0o077)
    root = Path('/var/lib/yeodam/nginx-rehearsal')/a.run_id
    if not (root/'plan.json').exists() or a.trip_id <= 0:
        p.error('Known capture and positive trip ID required')
    manifest = json.loads(a.manifest.read_text())
    sizes = [int(x['sizeBytes']) for x in manifest['files']]
    if len(sizes) != 150 or min(sizes) <= 0:
        p.error('Use the exact150-photo manifest')
    def sql(query):
        return subprocess.check_output(['mysql','--protocol=socket','--user=root','--database=yeodam_v1_source',
            '--batch','--raw','--skip-column-names','--execute',query],text=True).strip()
    owner = sql('SELECT u.email FROM trips t JOIN users u ON u.user_id=t.user_id WHERE t.trip_id='+str(a.trip_id)+';')
    if owner != 'mig-'+a.session_run_id+'-0000@staging.invalid':
        p.error('Only the exact synthetic upload owner is allowed')
    keys = sql('SELECT f.object_key FROM trip_attachments a JOIN files f ON f.file_id=a.file_id '
               'WHERE a.trip_id='+str(a.trip_id)+' AND a.deleted_at IS NULL AND f.deleted_at IS NULL ORDER BY a.trip_attachment_id;').splitlines()
    def head(key):
        r = subprocess.run(['aws','s3api','head-object','--region','ap-northeast-2',
            '--bucket','yeodam-v2-stg-v1-app-483175530259-ap-northeast-2','--key',key,
            '--query','ContentLength','--output','text'],capture_output=True,text=True,timeout=20)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(head,keys))
    result = {'tripId':a.trip_id,'expectedPhotos':150,'dbAttachments':len(keys),
        'processingStatus':sql('SELECT processing_status FROM trips WHERE trip_id='+str(a.trip_id)+';'),
        's3ObjectsFound':sum(x is not None for x in actual),'s3TotalBytes':sum(x or 0 for x in actual),
        'expectedOriginalBytes':sum(sizes),'s3SizeMultisetMatchesManifest':sorted(x for x in actual if x is not None)==sorted(sizes),
        'manifestSha256':hashlib.sha256(a.manifest.read_bytes()).hexdigest(),'contentChecksumCompared':False}
    output = root/'journey-audit.json'
    with output.open('x') as stream:
        json.dump(result,stream,indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
