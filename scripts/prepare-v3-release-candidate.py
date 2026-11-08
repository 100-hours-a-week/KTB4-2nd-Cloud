#!/usr/bin/env python3
"""Prepare/check public full-app release inputs; never deploy or publish a PR."""
import argparse,json,os
from pathlib import Path
import v3_release_bundle as bundle
from v3_delivery_contract import ROOT,CHARTS
from v3_release_candidate import PATH,verify_components


def chart_files():
 from prepare_v3_candidate_helpers import local_charts
 return local_charts()


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,default=ROOT/PATH)
 p.add_argument('--output',type=Path);p.add_argument('--verify-images',action='store_true');a=p.parse_args()
 data=bundle.parse_json(a.input.read_text())
 if data.get('state')=='unprepared'and not a.output:
  bundle.require(data=={'schemaVersion':2,'target':'yeodam-v3-foundation','state':'unprepared','components':{},'values':{},'serviceReview':{}},'Invalid bootstrap placeholder')
  print('V3 full candidate unprepared; no publication or deployment');return
 data=bundle.normalize(data)if a.output else bundle.prepared(data)
 files=bundle.build(data,chart_files(),{'candidatePr':0,'cloudCommit':'0'*40,'cloudGateRunId':0});bundle.validate(files)
 if a.verify_images:
  from v3_gitops_release import legacy
  verify_components(data,legacy.checks.api)
 if a.output:
  bundle.require(not a.output.exists(),'Refusing overwrite; review diff explicitly');os.umask(0o077);a.output.write_text(json.dumps(data,indent=2)+'\n')
 print('Full candidate: four pinned images, nine minimum app replicas and reference-only settings verified; no deployment')


if __name__=='__main__':main()
