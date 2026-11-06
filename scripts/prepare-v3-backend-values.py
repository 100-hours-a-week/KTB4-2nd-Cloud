#!/usr/bin/env python3
"""Generate reference-only first-auth/all-role overlays; no AWS, deployment or secret payloads."""
import argparse,json,re,os
from pathlib import Path
from urllib.parse import urlsplit
from prepare_v3_backend_values_contract import require,prepare

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=['auth','all'],required=True);p.add_argument('--image',required=True);p.add_argument('--release',required=True);p.add_argument('--configuration-revision',required=True);p.add_argument('--settings',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args();os.umask(0o077)
 require(not a.output.exists(),'Refusing to replace an existing deployment input')
 overlay=prepare(a.stage,a.image,a.release,json.loads(a.settings.read_text()),a.configuration_revision)
 a.output.parent.mkdir(parents=True,exist_ok=True)
 with a.output.open('x') as f:json.dump(overlay,f,indent=2)
 print('Reference-only '+a.stage+' deployment overlay prepared; no deployment performed')
if __name__=='__main__':main()
