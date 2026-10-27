#!/usr/bin/env python3
"""Bounded staging-only TCP/TLS occupancy and turnover; expected policy drops stay visible."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import resource
import platform
import signal
import time

HOST='v1-staging.yeodam-2gether.com'
ORIGIN='https://'+HOST


def now():
    return datetime.now(timezone.utc).isoformat()


def wire_request(close=False):
    return (b'GET /api/trips/map HTTP/1.1\r\nHost: '+HOST.encode()+
            b'\r\nUser-Agent: yeodam-tcp436\r\nConnection: '+
            (b'close' if close else b'keep-alive')+b'\r\n\r\n')


async def response(reader):
    header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
    if len(header)>16384:
        raise ValueError('Header too large')
    lines=header.decode('latin1').split('\r\n')
    code=int(lines[0].split()[1])
    fields=dict(line.split(':',1) for line in lines[1:] if ':' in line)
    fields={k.lower():v.strip().lower() for k,v in fields.items()}
    if 'content-length' in fields:
        size=int(fields['content-length'])
        if not 0<=size<=65536:
            raise ValueError('Body too large')
        await reader.readexactly(size)
    elif fields.get('transfer-encoding')=='chunked':
        total=0
        while True:
            size=int((await reader.readline()).split(b';')[0],16)
            if not size:
                for _ in range(32):
                    trailer=await reader.readline()
                    if trailer==b'\r\n':break
                    if not trailer:raise ValueError('Truncated trailers')
                else:raise ValueError('Too many trailers')
                break
            total+=size
            if total>65536:
                raise ValueError('Body too large')
            await reader.readexactly(size+2)
    else:
        raise ValueError('Unknown framing')
    return code,fields.get('connection')=='close'


async def exercise(a):
    start=time.monotonic();end=start+a.seconds
    context=ssl.create_default_context();context.set_alpn_protocols(['http/1.1'])
    rows=[];active=0;peak=0;status=Counter();errors=Counter();opened=0
    snapshots=[]
    stopped=asyncio.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:
        asyncio.get_running_loop().add_signal_handler(sig,stopped.set)
    async def client(index):
        nonlocal active,peak,opened
        row={'index':index,'startedAt':now()};writer=None;connected=False
        try:
            kwargs={} if a.mode=='tcp-hold' else {'ssl':context,'server_hostname':HOST,'ssl_handshake_timeout':3}
            reader,writer=await asyncio.wait_for(asyncio.open_connection(HOST,443,**kwargs),4)
            row['connectedAt']=now();opened+=1;active+=1;connected=True;peak=max(peak,active)
            if a.mode=='tcp-hold':
                row['outcome']='server_eof' if await asyncio.wait_for(reader.read(1),max(.1,end-time.monotonic()))==b'' else 'unexpected_bytes'
            else:
                while time.monotonic()<end and not stopped.is_set():
                    writer.write(wire_request())
                    await writer.drain()
                    code,closed=await asyncio.wait_for(response(reader),3);status[str(code)]+=1
                    if code not in (401,429):
                        row['unexpectedStatus']=code
                        errors['UnexpectedStatus']+=1
                    if a.mode=='churn' or closed:
                        row['outcome']='completed' if a.mode=='churn' else 'server_close'
                        break
                    await asyncio.sleep(min(8,max(0,end-time.monotonic())))
                row.setdefault('outcome','hold_completed')
        except asyncio.CancelledError:
            row['outcome']='cancelled'
            raise
        except asyncio.TimeoutError:
            if a.mode=='tcp-hold' and connected:
                row['outcome']='hold_completed'
            else:
                row['error']='TimeoutError';errors[row['error']]+=1
        except Exception as e:
            row['error']=type(e).__name__;errors[row['error']]+=1
            if isinstance(e, OSError):row['errno']=e.errno
        finally:
            if connected:active-=1
            if writer:
                writer.close()
                try:await asyncio.wait_for(writer.wait_closed(),.5)
                except Exception:pass
            row['endedAt']=now();rows.append(row)
    async def sample():
        while time.monotonic()<end and not stopped.is_set():
            snapshots.append({'utc':now(),'activeGeneratorSockets':active,'opened':opened,'responses':dict(status),'errors':dict(errors)})
            await asyncio.sleep(1)
    sampler=asyncio.create_task(sample());tasks=[];index=0
    while time.monotonic()<end and not stopped.is_set():
        if a.stop_file and a.stop_file.exists():
            break
        if a.mode!='churn' and index>=a.connections:
            await asyncio.sleep(.1);continue
        tasks.append(asyncio.create_task(client(index)));index+=1
        await asyncio.sleep(max(0,start+index/a.rate-time.monotonic()))
    if stopped.is_set() or (a.stop_file and a.stop_file.exists()):
        stopped.set()
        for task in tasks:task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True);await sampler
    recovery=[]
    for delay in [1,2,3]:
        if stopped.is_set():break
        await asyncio.sleep(delay)
        writer=None;item={'utc':now(),'delaySeconds':delay}
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_connection(HOST,443,ssl=context,server_hostname=HOST,ssl_handshake_timeout=3),4)
            writer.write(wire_request(close=True))
            await writer.drain();item['status'],_=await asyncio.wait_for(response(reader),3)
        except Exception as e:item['error']=type(e).__name__
        finally:
            if writer:writer.close()
        recovery.append(item)
    return {'schema':1,'startedAt':datetime.fromtimestamp(time.time()-(time.monotonic()-start),timezone.utc).isoformat(),
            'endedAt':now(),'mode':a.mode,'target':ORIGIN,'seconds':a.seconds,'openingRate':a.rate,
            'requestedConnections':a.connections,'attempted':len(rows),'opened':opened,'peakGeneratorSockets':peak,
            'httpStatuses':dict(status),'errors':dict(errors),'stopped':stopped.is_set(),
            'fdLimits':resource.getrlimit(resource.RLIMIT_NOFILE),'recoveryProbes':recovery,
            'rows':rows,'samples':snapshots,'scope':'TCP/TLS client results; policy drops are failures, not business successes; no application retries'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['hold','tcp-hold','churn'],required=True)
    p.add_argument('--connections',type=int,default=64)
    p.add_argument('--rate',type=int,required=True)
    p.add_argument('--seconds',type=int,required=True)
    p.add_argument('--confirm-target',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stop-file',type=Path)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--local-driver',action='store_true',help='Explicit macOS second source, no credentials; <=256 connections, <=80 attempts/s, <=900s')
    a=p.parse_args()
    duration_limit=900 if a.local_driver else 300
    if a.confirm_target!=ORIGIN or not 1<=a.connections<=3072 or not 1<=a.rate<=120 or not 10<=a.seconds<=duration_limit:
        p.error('Exact staging target and bounded connections/rate/time required')
    if a.mode!='churn' and a.connections/a.rate+5>a.seconds:
        p.error('Allow at least five seconds at target occupancy')
    if not a.execute:
        print(json.dumps({'mode':a.mode,'connections':a.connections,'rate':a.rate,'seconds':a.seconds,'execute':False}));return
    if a.local_driver:
        if platform.system()!='Darwin' or a.connections>256 or a.rate>80:
            p.error('Explicit macOS second source requires <=256 connections and <=80 attempts/s')
    elif os.geteuid()!=0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-0c53b65eb35dd2b42':
        p.error('Exact existing generator root only')
    os.umask(0o077)
    with a.output.open('x') as f:
        result=asyncio.run(exercise(a));json.dump(result,f)
    print(json.dumps({k:v for k,v in result.items() if k not in ['rows','samples']}))


if __name__=='__main__':
    main()
