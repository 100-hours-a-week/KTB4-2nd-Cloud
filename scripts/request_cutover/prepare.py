"""Create a private disposable runtime with fixed source and manifest images."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
from uuid import uuid4

from .config import private_file

V1_FE = 'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:8bcfc62c5b0f6cae7a620e7245cf50cb10199cdd59736088e570a38e46ba48d7'
V1_BE = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:a6a76c1f3f85910851221e7d8660f66c133ba30e2912cd330f8565be45755959'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    args.runtime.mkdir(mode=0o700, parents=True, exist_ok=False)
    run = str(uuid4())
    config = json.loads((args.root / 'deploy/request-cutover/config.example.json').read_text())
    config.update(run_id=run, public_host='yeodam-2gether.com',
                  socket='/run/yeodam-cutover/control.sock', journal='/run/yeodam-cutover/journal.json',
                  v2_origin='https://v2-ingress', v2_tls_name='yeodam-2gether.com',
                  upstream_ca_file='/private/tls/server.crt')
    config['redis']['prefix'] = f'yeodam:cutover:{run}:'
    manifest = json.loads((args.root / 'deploy/v2/image-manifest.json').read_text())
    images = {'CUTOVER_V1_BE_IMAGE': V1_BE, 'CUTOVER_V1_FE_IMAGE': V1_FE}
    for name, key in [('backend', 'CUTOVER_V2_BE_IMAGE'), ('frontend', 'CUTOVER_V2_FE_IMAGE')]:
        entry = manifest['components'][name]
        images[key] = entry['image'].split(':sha-', 1)[0] + '@' + entry['digest']
    values = {**images, 'CUTOVER_RUNTIME': str(args.runtime.resolve()),
              'CUTOVER_MYSQL_PASSWORD': secrets.token_hex(24),
              'CUTOVER_REDIS_PASSWORD': secrets.token_hex(24),
              'CUTOVER_JWT_SECRET': secrets.token_hex(32),
              'CUTOVER_AUTH_PREFIX': config['redis']['prefix']}
    private_file(args.runtime / 'runtime.env', ''.join(f'{k}={v}\n' for k, v in values.items()))
    private_file(args.runtime / 'config.json', json.dumps(config, indent=2) + '\n')
    private_file(args.runtime / 'image-lock.json', json.dumps(images, indent=2) + '\n')
    (args.runtime / 'state').mkdir(mode=0o700)
    tls = args.runtime / 'tls'
    tls.mkdir(mode=0o700)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-days', '2', '-subj', '/CN=yeodam-2gether.com',
        '-addext', 'subjectAltName=DNS:yeodam-2gether.com,DNS:localhost',
        '-keyout', str(tls / 'server.key'), '-out', str(tls / 'server.crt')],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (tls / 'server.key').chmod(0o600)
    (tls / 'server.crt').chmod(0o644)
    print(json.dumps({'run_id': run, 'runtime': str(args.runtime.resolve()),
                      'scope': 'isolated containers; no AWS mutation'}))


if __name__ == '__main__':
    main()
