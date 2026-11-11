"""API event-loop liveness tolerates dependency503; runner Ready is status, not a claim gate."""
import os,sys,urllib.request,urllib.error
from pathlib import Path

def api_live():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3) as r:return r.status==200
    except urllib.error.HTTPError as e:
        try:return e.code==503
        finally:e.close()

def runner_ready():
    from reference_data import tourapi_ready
    if not tourapi_ready():return False
    from redis import Redis
    path=Path(os.environ['PLACE_INDEX_PATH'])
    if not path.is_file() or not Path('/tmp/place-index-ready').is_file():return False
    with path.open('rb') as f:
        if f.read(16)!=b'SQLite format 3\x00':return False
    with Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=2,socket_timeout=2) as r:return bool(r.ping())
if __name__=='__main__':
    try:ok=api_live() if sys.argv[1]=='api-live' else runner_ready()
    except Exception:ok=False
    raise SystemExit(0 if ok else 1)
