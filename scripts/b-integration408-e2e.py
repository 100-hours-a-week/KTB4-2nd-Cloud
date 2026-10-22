#!/usr/bin/env python3
"""Require actual isolated V2 image startup before a measured #408 Run."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import uuid

ROOT=Path(__file__).resolve().parents[1]


def registry_ready(aws):
    definition=aws('ecs','describe-task-definition','--task-definition','yeodam-v2-staging-a-rehearsal-backend:1')['taskDefinition']
    container=definition['containerDefinitions'][0]
    secret=container['repositoryCredentials']['credentialsParameter']
    credentials=json.loads(aws('secretsmanager','get-secret-value','--secret-id',secret)['SecretString'])
    req=Request('https://ghcr.io/token?service=ghcr.io&scope=repository:100-hours-a-week/yeodam-backend:pull',
                headers={'Authorization':'Basic '+base64.b64encode((credentials['username']+':'+credentials['password']).encode()).decode()})
    try:
        with urlopen(req,timeout=15) as response:
            if response.status!=200:raise RuntimeError('Registry pull authorization failed; no service started')
    except HTTPError as error:
        raise RuntimeError('Registry pull authorization HTTP '+str(error.code)+'; no service started') from None
    return container['image']


def main():
    p=argparse.ArgumentParser();p.add_argument('--case',required=True,choices=('control','limited'));args=p.parse_args()
    spec=importlib.util.spec_from_file_location('compare408',ROOT/'scripts/run-b408-comparison.py')
    compare=importlib.util.module_from_spec(spec);spec.loader.exec_module(compare);m=compare.runner(args.case)
    if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
    image=registry_ready(m.ssm.aws)
    verdict={'healthy':False,'registryPullAuthorized':True,'image':image,'startedAt':time.time()}
    original=None
    try:
        original=m.service_start(str(uuid.uuid4()))
        deadline=time.monotonic()+420
        while time.monotonic()<deadline:
            service=m.ssm.aws('ecs','describe-services','--cluster',m.CLUSTER,'--services',m.SERVICE)['services'][0]
            # A failed candidate must not silently pass after ECS circuit-breaker rollback.
            expected=[d for d in service['deployments'] if d['status']=='PRIMARY']
            if (not expected or expected[0]['taskDefinition']==original) and time.time()-verdict['startedAt']>30:
                raise RuntimeError('Preflight service rolled back; no input')
            groups=[v['targetGroupArn'] for v in service['loadBalancers']]
            healthy=bool(groups)
            for group in groups:
                states=m.ssm.aws('elbv2','describe-target-health','--target-group-arn',group)['TargetHealthDescriptions']
                healthy=healthy and any(x['TargetHealth']['State']=='healthy' for x in states)
            if service['taskDefinition']!=original and service['runningCount']==1 and service['pendingCount']==0 and healthy:
                tasks=m.ssm.aws('ecs','list-tasks','--cluster',m.CLUSTER,'--service-name',m.SERVICE)['taskArns']
                actual=m.ssm.aws('ecs','describe-tasks','--cluster',m.CLUSTER,'--tasks',*tasks)['tasks'] if tasks else []
                matching=[t for t in actual if t['taskDefinitionArn']==service['taskDefinition'] and t['lastStatus']=='RUNNING'
                          and any(c.get('imageDigest')==image.split('@',1)[1] for c in t['containers'])]
                if matching:
                    verdict.update(healthy=True,taskDefinition=service['taskDefinition'],actualImageDigest=image.split('@',1)[1]);break
            time.sleep(5)
        if not verdict['healthy']:raise RuntimeError('V2 E2E service not Healthy before input')
    finally:
        if original is not None:m.service_stop(original)
        verdict['finishedAt']=time.time()
        (m.EVIDENCE/'e2e-preflight.json').write_text(json.dumps(verdict,indent=2))
    print(json.dumps(verdict))


if __name__=='__main__':main()
