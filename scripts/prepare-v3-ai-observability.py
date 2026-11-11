#!/usr/bin/env python3
"""Prepare reader/writer candidates. Writer review reads only V3 AI and Argo metadata."""
import argparse
import hashlib
import uuid
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import v3_release_bundle as bundle
from v3_ai_observability import prepare,readers,transition,require


def kube(config,*args):
    p=subprocess.run(['kubectl','--kubeconfig',str(config),'--request-timeout=20s',*args,'-o','json'],capture_output=True,timeout=30)
    require(p.returncode==0,'Reader review could not query Kubernetes; diagnostics omitted')
    return json.loads(p.stdout)


def review(config,commit,approved):
    live=subprocess.check_output(['git','ls-remote','origin','refs/heads/deploy/v3-approved'],text=True,stderr=subprocess.PIPE).split()
    require(len(live)==2 and live[0]==commit,'Approved ref changed; review a fresh reader snapshot')
    application=kube(config,'-n','argocd','get','application','yeodam-v3-apps')
    spec=importlib.util.spec_from_file_location('observe',Path(__file__).with_name('observe-v3-release.py'));observe=importlib.util.module_from_spec(spec);spec.loader.exec_module(observe)
    require(observe.status(application,commit)['deployed'],'Reader release must be observed Synced/Healthy')
    d=kube(config,'-n','ai','get','deployments')['items'];p=kube(config,'-n','ai','get','pods')['items']
    proof=readers(d,p,approved)
    for name in ['ai-api','ai-runner']:
        cm=kube(config,'-n','ai','get','configmap',name+'-runtime')['data']
        require(cm.get('JOBS_TRACE_CONTEXT_WRITE')=='false' and cm.get('RELEASE')==approved['components']['ai-app']['sourceSha'],'Effective reader settings/source mismatch')
    return {'approvedCommit':commit,'readers':proof,'secretRead':False}


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--phase',choices=['reader','writer'],required=True)
    p.add_argument('--runtime-secret-version',help='Public VersionId after the two fields are supplied; never a Secret value')
    p.add_argument('--approved-commit');p.add_argument('--kubeconfig',type=Path);p.add_argument('--review-output',type=Path)
    a=p.parse_args();source=bundle.parse_json(a.candidate.read_text());bundle.prepared(source)
    result=bundle.normalize(prepare(source,a.phase))
    if a.phase=='reader':
        require(a.runtime_secret_version and len(a.runtime_secret_version) in [32,36],'Reader candidate needs the supplied runtime Secret VersionId')
        uuid.UUID(a.runtime_secret_version)
        result['values']['ai']['configurationRevision']=hashlib.sha256(bundle.canonical({'previous':result['values']['ai']['configurationRevision'],'runtimeSecretVersion':a.runtime_secret_version,'observability':(bundle.ROOT/'deploy/v3/ai-apps/files/observability.json').read_text()})).hexdigest()
    if a.phase=='writer':
        require(a.approved_commit and a.kubeconfig and a.review_output,'Writer requires approved reader commit, kubeconfig and private review output')
        require(__import__('re').fullmatch('[a-f0-9]{40}',a.approved_commit),'Exact approved commit required')
        raw=subprocess.check_output(['git','show',a.approved_commit+':candidate.json'],stderr=subprocess.PIPE)
        approved=bundle.parse_json(raw);bundle.prepared(approved);transition(approved,result)
        proof=review(a.kubeconfig,a.approved_commit,approved)
        with a.review_output.open('x') as f:json.dump(proof,f,indent=2)
    bundle.prepared(result)
    # Render/contract validation is still required by candidate CI before deployment.
    with a.output.open('x') as f:f.write(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print('AI '+a.phase+' candidate prepared; no Secret read, deployment, model or GPU calls')


if __name__=='__main__':main()
