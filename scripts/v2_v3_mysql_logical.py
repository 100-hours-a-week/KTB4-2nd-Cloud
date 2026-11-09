"""Private logical backup packages; restores are restricted to labelled isolated Docker fixtures."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SOURCE_IMAGE = 'docker.io/library/mysql:9.7.2@sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497'
TARGET_IMAGE = 'public.ecr.aws/docker/library/mysql@sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902'
SCHEMA = 'yeodam'
DUMP_OPTIONS = ['--single-transaction','--quick','--skip-lock-tables','--no-tablespaces',
                '--set-gtid-purged=OFF','--column-statistics=0','--hex-blob','--skip-triggers',
                '--routines=false','--events=false','--skip-add-locks','--skip-comments']
ROOT_SHELL = 'export MYSQL_PWD="$(cat /credentials/root-password)"; exec mysql --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uroot --batch --raw --skip-column-names'
MAX_COMPRESSED = 256*1024*1024
MAX_SQL = 1024*1024*1024
NAME = re.compile(r'[a-z_][a-z0-9_]{0,63}')


def require(ok, message):
    if not ok:raise ValueError(message)


def docker(*args, source=None):
    r=subprocess.run(['docker',*map(str,args)],input=source,capture_output=True,text=True,timeout=180)
    if r.returncode:raise RuntimeError('Isolated Docker command failed; no SQL or credentials printed')
    return r.stdout


def isolated(container, side):
    fixture = side in ['source','target'] and re.fullmatch(r'v2-v3-restore-[a-f0-9]{10}-'+side,container)
    live_target = side == 'target' and re.fullmatch(r'v2-v3-live-restore-[a-f0-9]{10}-target',container)
    require(fixture or live_target, 'Only isolated rehearsal names allowed')
    issue = '593' if live_target else '591'
    d=json.loads(docker('inspect',container))[0]
    image=SOURCE_IMAGE if side=='source' else TARGET_IMAGE
    require(d['State']['Running'] and d['Config']['Image']==image
            and d['Config']['Labels'].get('yeodam.issue')==issue
            and not d['HostConfig'].get('PortBindings') and not d['HostConfig'].get('Privileged'), 'Container identity/exposure mismatch')
    networks=d['NetworkSettings']['Networks'];require(len(networks)==1,'One internal fixture network required')
    net=next(iter(networks));require(net==container+'-net','Wrong fixture network')
    n=json.loads(docker('network','inspect',net))[0]
    require(n['Internal'] and n['Labels'].get('yeodam.issue')==issue,'External network forbidden')


def sql(container, query):
    return docker('exec','-i',container,'/bin/bash','-ec',ROOT_SHELL,source=query).strip()


def identifier(value):
    require(isinstance(value,str) and NAME.fullmatch(value),'Invalid schema identifier')
    return '`'+value+'`'


def catalog(container):
    names=sql(container,"SELECT table_name FROM information_schema.tables WHERE table_schema='yeodam' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()
    return [name for name in names if identifier(name)]


def capture(container, shape=None, query_fn=None):
    """Hash canonical hex values without printing business rows or storing row payloads."""
    execute = query_fn or (lambda query: sql(container, query))
    out={}
    for table in sorted(shape if shape is not None else execute("SELECT table_name FROM information_schema.tables WHERE table_schema='yeodam' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()):
        name=identifier(table)
        columns=shape[table]['columns'] if shape is not None else execute("SELECT column_name FROM information_schema.columns WHERE table_schema='yeodam' AND table_name='"+table+"' ORDER BY ordinal_position;").splitlines()
        keys=shape[table]['primaryKey'] if shape is not None else execute("SELECT column_name FROM information_schema.key_column_usage WHERE table_schema='yeodam' AND table_name='"+table+"' AND constraint_name='PRIMARY' ORDER BY ordinal_position;").splitlines()
        require(columns and keys,'Every source table must have a primary key')
        projections=["IFNULL(CONCAT('V',HEX(CAST("+identifier(col)+" AS BINARY))),'N')" for col in columns]
        where=' WHERE CAST(version AS UNSIGNED)<=32' if table=='flyway_schema_history' else ''
        query='SELECT '+','.join(projections)+' FROM yeodam.'+name+where+' ORDER BY '+','.join(identifier(k) for k in keys)+';'
        raw=execute(query)
        count=int(execute('SELECT COUNT(*) FROM yeodam.'+name+where+';'))
        out[table]={'columns':columns,'primaryKey':keys,'rows':count,'sha256':hashlib.sha256(raw.encode()).hexdigest()}
    return out


def private_json(path, value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(value,f,indent=2)


def file_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def verify_package(directory):
    require(directory.is_dir() and not directory.is_symlink(),'Invalid package directory')
    require({p.name for p in directory.iterdir()}=={'backup.json','yeodam.sql.gz'},'Exact package files required')
    manifest,dump=directory/'backup.json',directory/'yeodam.sql.gz'
    require(all(p.is_file() and not p.is_symlink() for p in [manifest,dump]),'Symlink package rejected')
    d=json.loads(manifest.read_text())
    require(d['formatVersion']==1 and d['schema']=='yeodam' and d['sourceEngine']=='9.7.2'
            and d['sourceSchemaVersion']==32 and d['sourceFrozen'] is True
            and d['sensitivity'] in ['synthetic','private-user-data'],'Backup provenance mismatch')
    require(not os.environ.get('GITHUB_ACTIONS') or d['sensitivity']=='synthetic','Private backups must not enter GitHub CI')
    require(type(d['compressedBytes']) is int and 0<d['compressedBytes']<=MAX_COMPRESSED
            and dump.stat().st_size==d['compressedBytes'] and file_digest(dump)==d['sha256'],'Backup integrity mismatch')
    size=0
    with gzip.open(dump,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            size+=len(chunk);require(size<=MAX_SQL,'SQL stream exceeds reviewed size')
    return d


def backup(container, directory):
    isolated(container,'source')
    require(sql(container,'SELECT VERSION();')=='9.7.2','Source engine mismatch')
    require(sql(container,'SELECT @@global.read_only AND @@global.super_read_only;')=='1','Freeze source before backup')
    require(sql(container,"SELECT CONCAT(COUNT(*),':',SUM(success),':',COUNT(DISTINCT version),':',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history;")=='32:32:32:32','Expected V32 history')
    unsupported="SELECT (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.events WHERE event_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam' AND (table_type<>'BASE TABLE' OR engine<>'InnoDB'));"
    require(sql(container,unsupported)=='0','Non-InnoDB/view/trigger/routine/event needs separate review')
    snapshots=capture(container)
    directory.mkdir(mode=0o700)
    archive=directory/'yeodam.sql.gz'
    command='export MYSQL_PWD="$(cat /credentials/root-password)"; exec mysqldump --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uroot '+' '.join(DUMP_OPTIONS)+' yeodam'
    proc=None
    try:
        with tempfile.TemporaryFile() as err:
            proc=subprocess.Popen(['docker','exec',container,'/bin/bash','-ec',command],stdout=subprocess.PIPE,stderr=err)
            with os.fdopen(os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as raw:
                with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as output:
                    total=0
                    for chunk in iter(lambda:proc.stdout.read(65536),b''):
                        total+=len(chunk);require(total<=MAX_SQL,'Dump size exceeds review');output.write(chunk)
            require(proc.wait(timeout=180)==0,'Logical dump failed; private stderr suppressed')
        require(capture(container)==snapshots,'Source changed during backup')
        d={'formatVersion':1,'schema':'yeodam','sourceEngine':'9.7.2','sourceSchemaVersion':32,
           'sourceFrozen':True,'sensitivity':'synthetic','compressedBytes':archive.stat().st_size,
           'sha256':file_digest(archive),'sourceUuid':sql(container,'SELECT @@server_uuid;'),'tables':snapshots}
        private_json(directory/'backup.json',d);verify_package(directory)
        return d
    except Exception:
        if proc and proc.poll() is None:proc.kill();proc.wait()
        # Retain no partial dump that could be mistaken for a usable backup.
        archive.unlink(missing_ok=True);raise


def restore(container, directory):
    d=verify_package(directory);isolated(container,'target')
    require(sql(container,'SELECT VERSION();')=='8.4.13','Target engine mismatch')
    require(sql(container,'SELECT @@server_uuid;')!=d['sourceUuid'],'Source and target must differ')
    require(not catalog(container),'Refusing to overwrite a nonempty target schema')
    command=ROOT_SHELL+' yeodam'
    proc=None
    try:
        with tempfile.TemporaryFile() as err:
            proc=subprocess.Popen(['docker','exec','-i',container,'/bin/bash','-ec',command],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=err)
            with gzip.open(directory/'yeodam.sql.gz','rb') as source:
                for chunk in iter(lambda:source.read(65536),b''):proc.stdin.write(chunk)
            proc.stdin.close();require(proc.wait(timeout=180)==0,'Logical restore failed; private stderr suppressed')
    finally:
        if proc and proc.poll() is None:proc.kill();proc.wait()
    require(capture(container,d['tables'])==d['tables'],'Restored legacy rows/history differ')
    return {'restored':True,'schema':SCHEMA,'legacyTablesCompared':len(d['tables']),'rowContentsMatch':True}


def foreign_key_violations(container):
    rows=sql(container,"SELECT JSON_OBJECT('table',table_name,'name',constraint_name,'column',column_name,'parent',referenced_table_name,'parentColumn',referenced_column_name) FROM information_schema.key_column_usage WHERE table_schema='yeodam' AND referenced_table_name IS NOT NULL ORDER BY table_name,constraint_name,ordinal_position;").splitlines()
    groups={}
    for line in rows:
        item=json.loads(line);groups.setdefault((item['table'],item['name'],item['parent']),[]).append(item)
    total=0
    for (table,name,parent),items in groups.items():
        join=' AND '.join('c.'+identifier(i['column'])+'=p.'+identifier(i['parentColumn']) for i in items)
        present=' AND '.join('c.'+identifier(i['column'])+' IS NOT NULL' for i in items)
        query='SELECT COUNT(*) FROM yeodam.'+identifier(table)+' c LEFT JOIN yeodam.'+identifier(parent)+' p ON '+join+' WHERE '+present+' AND p.'+identifier(items[0]['parentColumn'])+' IS NULL;'
        total+=int(sql(container,query))
    return total
