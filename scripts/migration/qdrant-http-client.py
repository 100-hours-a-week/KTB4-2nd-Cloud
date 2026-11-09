#!/usr/bin/env python3
"""Disposable internal-network TLS client. Used only with locally generated credentials."""
import http.client,json,ssl,sys
from pathlib import Path

def main():
    args=json.loads(sys.stdin.buffer.read())
    context=ssl.create_default_context(cafile='/config/tls.crt')
    connection=http.client.HTTPSConnection('qdrant-599.internal',6333,context=context,timeout=180)
    headers={'Content-Type':'application/json'}
    if args.get('authenticated',True):headers['api-key']=Path('/config/api-key').read_text().strip()
    try:
        body=json.dumps(args['body'],allow_nan=False).encode() if args.get('body') is not None else None
        connection.request(args['method'],args['path'],body=body,headers=headers)
        response=connection.getresponse()
        if args.get('download'):
            if response.status!=200:raise ValueError('Download refused')
            size=0
            while chunk:=response.read(512*1024):
                size+=len(chunk)
                if size>512*1024*1024:raise ValueError('Snapshot exceeds bound')
                sys.stdout.buffer.write(chunk)
        else:
            raw=response.read(16*1024*1024+1)
            if len(raw)>16*1024*1024:raise ValueError('Response exceeds bound')
            value=json.loads(raw) if response.status==200 else None
            print(json.dumps({'httpStatus':response.status,'value':value},allow_nan=False))
    finally:connection.close()
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Isolated HTTP request failed; private data suppressed') from None
