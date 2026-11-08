"""Merge public roots only; no application secrets or network calls."""
from pathlib import Path
import ssl
roots=Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()
for name in ['redis','qdrant','kafka']:
    ca=Path('/etc/yeodam/ca',name+'.crt').read_bytes()
    if b'PRIVATE KEY' in ca:raise SystemExit('Public certificates only')
    roots+=b'\n'+ca
ssl.create_default_context(cadata=roots.decode())
Path('/etc/yeodam/trust/ca-bundle.pem').write_bytes(roots)
