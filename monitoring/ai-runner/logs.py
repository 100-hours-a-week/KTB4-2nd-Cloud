#!/usr/bin/env python3
"""Normalize only the two fixed runner Docker logs into stable, bounded JSONL files."""
import datetime, fcntl, json, logging, logging.handlers, os, re, subprocess, time
from pathlib import Path
NAMES={'staging':'yeodam-ai-process-runner','production':'yeodam-ai-process-runner-production'}
STATE=Path('/var/lib/yeodam/ai-runner-logs')
OUTPUT=Path('/var/log/yeodam/ai-runner')
class StrictHandler(logging.handlers.RotatingFileHandler):
    def handleError(self,record):raise RuntimeError('Normalized log write failed')
SECRET=re.compile(r'(?i)^(authorization|password|api_key|access_token|refresh_token|secret|.*_api_key)$')
def redact(value):
    if isinstance(value,dict):return {k:'[REDACTED]' if SECRET.fullmatch(k) else redact(v) for k,v in value.items()}
    if isinstance(value,list):return [redact(v) for v in value]
    if not isinstance(value,str):return value
    value=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[REDACTED]',value)
    return re.sub(r'(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s"\\]+',r'\1[REDACTED]',value)
def normalize(event,env):
    line=event['log'].rstrip()
    try:body=json.loads(line)
    except ValueError:body={'message':line}
    if not isinstance(body,dict):body={'message':line}
    body.setdefault('timestamp',event['time']);body.update(env=env,service='ai-runner',docker_timestamp=event['time'])
    return json.dumps(redact(body),ensure_ascii=False,separators=(',',':'))
def valid_log(value):
    return bool(re.fullmatch(r'/var/lib/docker/containers/([0-9a-f]{64})/\1-json.log',value))
def collect(env,name):
    p=subprocess.run(['docker','inspect','--format','{{json .}}',name],capture_output=True,text=True,timeout=5,check=True)
    row=json.loads(p.stdout);path=Path(row['LogPath'])
    if row['HostConfig']['LogConfig']['Type']!='json-file' or not valid_log(str(path)):raise RuntimeError('Unexpected runner log source')
    checkpoint=STATE/(env+'.json');state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    previous=state.get('path');files=[]
    # Retain unread bytes from a still-existing previous container, then follow current .1 rotation.
    if previous and previous!=str(path) and valid_log(previous) and Path(previous).exists():files.append((Path(previous),state.get('position',0)))
    stat=path.stat();offset=0
    if previous==str(path) and state.get('inode')==stat.st_ino:offset=state.get('position',0)
    elif previous==str(path):
        old=Path(str(path)+'.1')
        if old.exists() and old.stat().st_ino==state.get('inode'):files.append((old,state.get('position',0)))
    files.append((path,offset if offset<=stat.st_size else 0))
    handler=StrictHandler(OUTPUT/(env+'.jsonl'),maxBytes=20*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'));logger=logging.getLogger(env);logger.handlers=[handler];logger.setLevel(logging.INFO);logger.propagate=False
    count=0;next_position=offset
    try:
        for file,start in files:
            with file.open('rb') as stream:
                stream.seek(min(start,file.stat().st_size));end=stream.tell()
                while end-start<2*1024*1024:
                    line=stream.readline(262145)
                    if not line or not line.endswith(b'\n'):break
                    end=stream.tell()
                    if len(line)>262144:continue
                    event=json.loads(line)
                    # Initial backfill is bounded to one hour. Preserve source timestamp in the record.
                    if not state and datetime.datetime.fromisoformat(event['time'].replace('Z','+00:00')).timestamp()<time.time()-3600:continue
                    logger.info(normalize(event,env));count+=1
            if file==path:next_position=end
        handler.flush();fd=os.open(OUTPUT/(env+'.jsonl'),os.O_RDONLY);os.fsync(fd);os.close(fd)
        temporary=checkpoint.with_suffix('.next');temporary.write_text(json.dumps({'path':str(path),'inode':stat.st_ino,'position':next_position}));temporary.chmod(0o600);temporary.replace(checkpoint)
    finally:handler.close()
    return count
if __name__=='__main__':
    os.umask(0o077);STATE.mkdir(parents=True,exist_ok=True);OUTPUT.mkdir(parents=True,exist_ok=True)
    with (STATE/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        results={}
        for env,name in NAMES.items():
            try:results[env]={'success':True,'lines':collect(env,name)}
            except Exception:results[env]={'success':False}
        print(json.dumps(results))
        if not all(x['success'] for x in results.values()):raise SystemExit(1)
