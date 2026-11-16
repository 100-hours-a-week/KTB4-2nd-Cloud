#!/usr/bin/env python3
"""Offline policy preparation/assessment; image inspection creates no running container.

Does not pull images, access Secrets, change Kubernetes, inject faults, or start GPU.
"""
import argparse,json
from pathlib import Path
import v3_cdc_fault as f
import v3_conversion_lease as lease


def load(path):
    f.require(path.is_file() and not path.is_symlink() and path.stat().st_size<=16*1024*1024,'Bounded regular input required')
    return json.loads(path.read_text())


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    a=sub.add_parser('inspect');a.add_argument('--source',required=True)
    group=a.add_mutually_exclusive_group(required=True);group.add_argument('--image');group.add_argument('--jar',type=Path)
    a=sub.add_parser('budget');a.add_argument('--metrics',type=Path,required=True);a.add_argument('--pause-allowance-seconds',type=float,required=True);a.add_argument('--rationale',required=True)
    a=sub.add_parser('candidate');a.add_argument('--candidate',type=Path,required=True);a.add_argument('--support',type=Path,required=True)
    a.add_argument('--duration-seconds',type=int,required=True);a.add_argument('--budget',type=Path)
    a=sub.add_parser('review');a.add_argument('--candidate',type=Path,required=True);a.add_argument('--support',type=Path,required=True)
    a.add_argument('--approved-revision',required=True);a.add_argument('--budget',type=Path)
    a=sub.add_parser('assess');a.add_argument('--plan',type=Path,required=True);a.add_argument('--directory',type=Path,required=True);a.add_argument('--trace',type=Path,required=True);a.add_argument('--metrics',type=Path,required=True)
    a=sub.add_parser('compare');a.add_argument('--baseline',type=Path,required=True);a.add_argument('--candidate-result',type=Path,required=True)
    a.add_argument('--baseline-metrics',type=Path,required=True);a.add_argument('--candidate-metrics',type=Path,required=True)
    for a in sub.choices.values():a.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    f.require(not args.output.exists() and not args.output.is_symlink() and args.output.parent.is_dir() and args.output.parent.stat().st_mode&0o077==0,'New output in private mode-700 directory required')
    if args.action=='inspect':result=lease.inspect_image(args.image,args.source)if args.image else lease.inspect_jar(args.jar,args.source)
    elif args.action=='budget':result=lease.measured_budget(load(args.metrics),args.pause_allowance_seconds,args.rationale)
    elif args.action=='candidate':result=lease.candidate(load(args.candidate),load(args.support),args.duration_seconds,load(args.budget)if args.budget else None)
    elif args.action=='review':result=lease.review(load(args.candidate),load(args.support),args.approved_revision,load(args.budget)if args.budget else None)
    elif args.action=='assess':result=lease.assess(load(args.plan),args.directory,load(args.trace),load(args.metrics))
    else:
        before,after=load(args.baseline),load(args.candidate_result)
        for result,path in [(before,args.baseline_metrics),(after,args.candidate_metrics)]:
            metrics=load(path)
            f.require(result.get('metricsSourceSha256')==f.sha(metrics) and
                      result.get('conditionsFingerprint')==lease.metrics_conditions(metrics),'Actual bound conditions evidence required')
        result=lease.compare(before,after,True)
    f.private_write(args.output,result)
    print(json.dumps({'prepared':True,'operatingChanges':False,'gpuChanged':False}))

if __name__=='__main__':main()
