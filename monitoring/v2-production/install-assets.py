#!/usr/bin/env python3
"""SSM entry: enforce destination instance and fixed artifact roots before writes."""
import base64,json,os,subprocess,urllib.request
from pathlib import Path
assert os.geteuid()==0
token=urllib.request.urlopen(urllib.request.Request('http://169.254.169.254/latest/api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
actual=urllib.request.urlopen(urllib.request.Request('http://169.254.169.254/latest/meta-data/instance-id',headers={'X-aws-ec2-metadata-token':token}),timeout=3).read().decode()
assert actual==os.environ['EXPECTED_INSTANCE_ID'],'Wrong observer destination'
files=json.loads(base64.b64decode(os.environ['OBS_ASSETS_B64']))
roots=[Path('/opt/yeodam/plg'),Path('/usr/local/lib/yeodam-observability')]
for name,content in files.items():
 path=Path(name);assert not path.is_symlink() and any(root in path.parents for root in roots)
 path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
 temporary=path.with_suffix(path.suffix+'.next');temporary.write_text(content);temporary.chmod(0o644);temporary.replace(path)
entry=os.environ.get('OBS_ENTRY','')
if entry:
 assert Path(entry).parent==roots[1]
 subprocess.run(['bash',entry],check=True)
print('Production observation artifacts ready:',len(files))
