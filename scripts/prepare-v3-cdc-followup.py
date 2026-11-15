#!/usr/bin/env python3
"""Prepare reviewed C0/C1/C2 plans; default subcommands never inject or start a GPU."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import v3_cdc_fault as f
import v3_cdc_followup as follow


def load(path):
    f.require(not path.is_symlink() and path.stat().st_size<=4*1024*1024,'Invalid input file')
    return json.loads(path.read_text())


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    a=sub.add_parser('snapshot',help='Read-only identities and Kafka timer overrides; no Secret reads')
    a.add_argument('--kubeconfig',required=True,type=Path);a.add_argument('--output',required=True,type=Path)
    a.add_argument('--controller-node',required=True);a.add_argument('--allow-worker-controller',action='store_true')
    a=sub.add_parser('plan',help='Offline inactive plan only')
    a.add_argument('--snapshot',required=True,type=Path);a.add_argument('--output',required=True,type=Path)
    a.add_argument('--case',choices=['C0','C1','C2'],required=True);a.add_argument('--run-id',required=True)
    a.add_argument('--c0-result',type=Path)
    a=sub.add_parser('collect',help='Read-only one-trial IDs/attempts/receipt/Trace; no payloads')
    a.add_argument('--kubeconfig',required=True,type=Path);a.add_argument('--trip-id',required=True,type=int)
    a.add_argument('--output',required=True,type=Path);a.add_argument('--ack-trial-metadata-read',action='store_true',required=True)
    a=sub.add_parser('observe',help='Bounded read-only DB sampling; no fault or GPU lifecycle')
    a.add_argument('--kubeconfig',required=True,type=Path);a.add_argument('--trip-id',required=True,type=int)
    a.add_argument('--directory',required=True,type=Path)
    a.add_argument('--seconds',required=True,type=int,help='Whole observation budget, 1..960 seconds')
    a.add_argument('--ack-trial-metadata-read',action='store_true',required=True)
    a=sub.add_parser('reconcile',help='Offline ID-set/receipt/Trace coverage; never claim exactly-once')
    for name in ('accepted','final','trace','output'): a.add_argument('--'+name,required=True,type=Path)
    a=sub.add_parser('assess',help='Offline timing/identity report with source hashes and explicit unknowns')
    a.add_argument('--case',choices=['C0','C1','C2'],required=True)
    for name in ('accepted','final','trace','timing','output'): a.add_argument('--'+name,required=True,type=Path)
    a.add_argument('--db-observation-summary',type=Path)
    a=sub.add_parser('run',help='Explicit approved fault execution; recovery runs in finally')
    a.add_argument('--kubeconfig',required=True,type=Path);a.add_argument('--plan',required=True,type=Path)
    a.add_argument('--approve-plan-sha256',required=True);a.add_argument('--ack-shared-cdc-maintenance',action='store_true',required=True)
    a.add_argument('--prometheus',required=True);a.add_argument('--directory',required=True,type=Path)
    a.add_argument('--trip-id',required=True,type=int)
    a.add_argument('--db-observation-directory',type=Path,
                   help='Read-only observer interlock: failure or stale status blocks/recovers injection')
    args=p.parse_args()
    if args.action=='observe':
        f.require(1<=args.seconds<=960,'Observation budget must be 1..960 seconds')
        k=f.Kube(args.kubeconfig)
        observer=follow.DbObserver(k,f.snapshot(k),args.trip_id,args.directory)
        deadline=time.monotonic()+args.seconds
        state='incomplete'
        try:
            while time.monotonic()<deadline:
                # Share the whole deadline with the per-sample budget; no renewal on retry.
                observer.SAMPLE_SECONDS=min(45,deadline-time.monotonic())
                proof=observer.sample()
                execution=next(r for r in proof['rows'] if r['section']=='execution_timing')
                if execution['status'] in ('COMPLETED','FAILED','CANCELLED','EXPIRED'):
                    state='completed' if execution['status']=='COMPLETED' else 'business_terminal_failure'
                    break
                time.sleep(min(3,max(0,deadline-time.monotonic())))
        except follow.DbObservationUnavailable:
            state='observation_failed'
            raise
        finally:
            observer.status(state)
            summary=dict(observer.summary(state),observationBudgetSeconds=args.seconds,pollIntervalSeconds=3)
            f.private_write(args.directory/'summary.json',summary)
        f.require(state=='completed','DB observation ended without confirmed completion; evidence preserved')
        print(json.dumps({'completed':True,'continuousObservationComplete':not observer.gaps,
                          'observationGaps':observer.gaps}));return
    if args.action=='run':
        f.require(args.directory.is_dir() and not args.directory.is_symlink() and
                  args.directory.stat().st_mode&0o077==0 and not any(args.directory.iterdir()),'Use empty mode-700 execution directory')
        plan=load(args.plan)
        metrics=follow.Observer(args.prometheus,args.directory)
        def healthy():
            return ((not args.db_observation_directory or follow.db_observer_ready(
                args.db_observation_directory,args.trip_id,plan['plans'][0]['snapshot']['approvedRevision'])) and metrics())
        result=follow.run(f.Kube(args.kubeconfig),plan,args.approve_plan_sha256,args.directory,
                          healthy,[args.trip_id])
        print(json.dumps({'case':result['case'],'businessRecoveryVerified':False}));return
    f.require(not args.output.exists() and not args.output.is_symlink(),'Output already exists')
    if args.action=='snapshot':
        result=follow.kafka_snapshot(f.Kube(args.kubeconfig),args.controller_node,args.allow_worker_controller)
    elif args.action=='collect':
        result=follow.collect(f.Kube(args.kubeconfig),[args.trip_id])
    elif args.action=='assess':
        accepted,final,timing=load(args.accepted),load(args.final),load(args.timing)
        if args.db_observation_summary:
            timing=follow.with_db_observations(timing,load(args.db_observation_summary),accepted,final)
        result=follow.assess(args.case,accepted,final,load(args.trace),timing)
    elif args.action=='reconcile':
        result=follow.reconcile(load(args.accepted),load(args.final),load(args.trace))
    else:
        result=follow.prepare(load(args.snapshot),args.run_id,args.case,load(args.c0_result) if args.c0_result else None)
    f.private_write(args.output,result)
    print(json.dumps({'prepared':True,'injected':False,'planSha256':result.get('planSha256')}))


if __name__=='__main__':
    try:main()
    except (ValueError,TypeError,KeyError,OSError,subprocess.SubprocessError) as e:
        print(json.dumps({'status':'blocked','reason':str(e) if isinstance(e,f.GuardError) else 'Raw diagnostics withheld'}),file=sys.stderr)
        sys.exit(1)
