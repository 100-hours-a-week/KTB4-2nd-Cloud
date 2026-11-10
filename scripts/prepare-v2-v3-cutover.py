#!/usr/bin/env python3
"""Create a private exact-byte cutover plan or a public operating-domain candidate. No live writes."""
import argparse,json,os
from pathlib import Path
import v2_v3_cutover as c
p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
a=sub.add_parser('plan')
for name in ['mysql-package','qdrant-package','photos-receipt','reconciliation','output']:a.add_argument('--'+name,type=Path,required=True)
a=sub.add_parser('candidate');a.add_argument('--source',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
a=p.parse_args();os.umask(0o077)
if a.action=='plan':
 value=c.plan(a.mysql_package,a.qdrant_package,a.photos_receipt,c.load(a.reconciliation));c.write(a.output,value);print(json.dumps({'planSha256':value['planSha256'],'phases':value['phases'],'resourcesChanged':False}))
else:
 c.write(a.output,c.operating_candidate(c.load(a.source)));print('Prepared operating config only; image digests and resource limits preserved')
