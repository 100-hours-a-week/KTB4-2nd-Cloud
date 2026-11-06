#!/usr/bin/env python3
"""Download only checksum-pinned BE migration SQL; never read AWS or registry credentials."""
import argparse,hashlib,json,shutil,tarfile,urllib.request
from pathlib import Path,PurePosixPath
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'deploy/v3/mysql-schema-image'
LOCK=json.loads((SOURCE/'source.lock.json').read_text())
def prepare(directory):
 directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
 if directory.is_symlink():raise ValueError('Symlink context rejected')
 sql=directory/'sql';sql.mkdir(exist_ok=True)
 if sql.is_symlink():raise ValueError('Symlink SQL directory rejected')
 archive=directory/'source.tar.gz'
 if archive.is_symlink():raise ValueError('Symlink archive rejected')
 expected='https://codeload.github.com/'+LOCK['sourceRepository']+'/tar.gz/'+LOCK['sourceCommit']
 if LOCK['archiveUrl']!=expected:raise ValueError('Unreviewed source URL')
 if not archive.exists():archive.write_bytes(urllib.request.urlopen(expected,timeout=60).read())
 data=archive.read_bytes()
 if len(data)!=LOCK['archiveBytes'] or hashlib.sha256(data).hexdigest()!=LOCK['archiveSha256']:raise ValueError('BE archive checksum mismatch')
 found={}
 with tarfile.open(archive) as tar:
  for m in tar.getmembers():
   p=PurePosixPath(m.name)
   if p.is_absolute() or '..' in p.parts or not(m.isfile() or m.isdir()):raise ValueError('Unsafe source archive')
   if '/src/main/resources/db/migration/' in m.name and m.isfile():
    if p.name in found:raise ValueError('Duplicate migration')
    found[p.name]=tar.extractfile(m).read()
 if set(found)!={x['name'] for x in LOCK['migrations']}:raise ValueError('Migration set mismatch')
 for entry in LOCK['migrations']:
  b=found[entry['name']];p=sql/entry['name']
  if p.is_symlink() or len(b)!=entry['bytes'] or hashlib.sha256(b).hexdigest()!=entry['sha256']:raise ValueError('Migration integrity failure')
  p.write_bytes(b);p.chmod(0o644)
 if {p.name for p in sql.iterdir()}!=set(found):raise ValueError('Stale migration files')
 jdbc=LOCK['jdbcArtifact'];target=directory/'mysql-jdbc.jar'
 if target.is_symlink():raise ValueError('Symlink JDBC artifact rejected')
 if not target.exists():target.write_bytes(urllib.request.urlopen(jdbc['url'],timeout=60).read())
 data=target.read_bytes()
 if len(data)!=jdbc['bytes'] or hashlib.sha256(data).hexdigest()!=jdbc['sha256']:raise ValueError('JDBC artifact checksum mismatch')
 for name in ['Dockerfile','source.lock.json','migrate.sh','checksums.sha256','.dockerignore']:
  if (directory/name).is_symlink():raise ValueError('Symlink image context file rejected')
 for name in ['Dockerfile','source.lock.json','migrate.sh']:shutil.copyfile(SOURCE/name,directory/name)
 (directory/'checksums.sha256').write_text(''.join(x['sha256']+'  '+x['name']+'\n' for x in LOCK['migrations']))
 (directory/'.dockerignore').write_text('*\n!Dockerfile\n!sql/\n!sql/**\n!source.lock.json\n!checksums.sha256\n!migrate.sh\n!mysql-jdbc.jar\n')
 print('Prepared pinned BE V1-V34 migration image context; no credentials included')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True,type=Path);prepare(p.parse_args().directory)
