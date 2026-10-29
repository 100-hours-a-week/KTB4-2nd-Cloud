#!/usr/bin/env bash
# No AWS calls, real tokens, shared generator or published host ports.
set -Eeuo pipefail
cd "$(dirname "$0")/../.."
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "${fixture_dir}"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=v1-staging.yeodam-2gether.com' \
  -keyout "${fixture_dir}/key.pem" -out "${fixture_dir}/cert.pem" 2>/dev/null
python3 - "${fixture_dir}" <<'PY'
from pathlib import Path
import importlib.util,json,sys
root=Path.cwd();folder=Path(sys.argv[1]);sys.path.insert(0,str(root/'monitoring/v1-staging'))
from nginx_user_read_profile import BACKEND_DIGEST
s=importlib.util.spec_from_file_location('renderer',root/'scripts/render-v1-staging-nginx.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
site=m.render((root/'nginx/yeodam.conf').read_text(),'a'*40,user_read_enabled=True,backend_digest=BACKEND_DIGEST,jwt_read_gate=True)
site=site.replace('/etc/letsencrypt/live/v1-staging.yeodam-2gether.com/fullchain.pem','/work/cert.pem').replace('/etc/letsencrypt/live/v1-staging.yeodam-2gether.com/privkey.pem','/work/key.pem')
site=site.replace('/etc/yeodam/nginx-jwt-gate/gate.mjs','/work/gate.mjs').replace('/etc/yeodam/nginx-jwt-gate/config.json','/work/config.json')
# Add no runtime route changes; use the whole rendered staging site.
(folder/'site.conf').write_text(site)
(folder/'gate.mjs').write_bytes((root/'monitoring/v1-staging/jwt-gate/gate.mjs').read_bytes())
(folder/'check.py').write_bytes((root/'tests/ci/check-nginx-jwt-gate.py').read_bytes())
(folder/'config.json').write_text(json.dumps({'secret':'synthetic-only-secret-for-local-fixture-468','issuer':'https://issuer.test','audience':'yeodam-api','clockSkewSeconds':60}))
(folder/'nginx.conf').write_text('load_module /usr/lib/nginx/modules/ngx_http_js_module.so;\nuser www-data;\nworker_processes 2;\nevents { worker_connections 1536; }\nhttp { include /work/site.conf; }\n')
PY
chmod 0755 "${fixture_dir}"
chmod 0644 "${fixture_dir}/gate.mjs" "${fixture_dir}/site.conf" "${fixture_dir}/nginx.conf" "${fixture_dir}/cert.pem" "${fixture_dir}/check.py"
# The root master preloads this synthetic key. Workers cannot read the key file.
chmod 0600 "${fixture_dir}/key.pem" "${fixture_dir}/config.json"
# The package versions are fixed in the Dockerfile. Build/pull is preparation only.
if ! docker image inspect yeodam-nginx-jwt-468:local >/dev/null 2>&1; then
  docker build --platform linux/amd64 -f tests/ci/nginx-jwt-gate.Dockerfile -t yeodam-nginx-jwt-468:local .
fi
docker run --rm --platform linux/amd64 --network none -v "${fixture_dir}:/work:ro" yeodam-nginx-jwt-468:local nginx -t -c /work/nginx.conf
docker run --rm --platform linux/amd64 --network none -v "${fixture_dir}:/work:ro" yeodam-nginx-jwt-468:local python3 /work/check.py
