#!/usr/bin/env python3
"""Review only two optional ESO readers plus the exact additive assume-role policy update."""
import argparse,json
from pathlib import Path
CREATES={f'aws_iam_role.reader["{n}"]' for n in ['ai-redis','qdrant']}|{f'aws_iam_role_policy.read["{n}"]' for n in ['ai-redis','qdrant']}
UPDATE='aws_iam_role_policy.assume_readers'
ARNS={f'arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-{n}' for n in ['ai-redis','qdrant']}
def validate(plan):
    if not plan.get('complete'):raise ValueError('Complete Plan required')
    changes=[r for r in plan.get('resource_changes',[]) if r.get('mode')=='managed' and r['change']['actions']!=['no-op']]
    if not changes:return {'creates':0,'updates':0,'deletes':0}
    if {r['address'] for r in changes}!=CREATES|{UPDATE} or len(changes)!=5:raise ValueError('Only two new readers and additive assume policy allowed; preserve all applied flags')
    for r in changes:
        c=r['change'];a=r['address'];after=c['after']
        if a==UPDATE:
            if c['actions']!=['update']:raise ValueError('Existing assume policy update only')
            before=json.loads(c['before']['policy']);new=json.loads(after['policy']);oldResources=set(before['Statement'][0]['Resource']);newResources=set(new['Statement'][0]['Resource'])
            if newResources-oldResources!=ARNS or oldResources-newResources:raise ValueError('Reader access must be additive and exact')
            oldResourcesList=before['Statement'][0]['Resource'];new['Statement'][0]['Resource']=oldResourcesList
            if new!=before:raise ValueError('Do not change other assume-role policy attributes')
        else:
            if c['actions']!=['create']:raise ValueError('No updates or deletes to readers')
            n='ai-redis' if '"ai-redis"' in a else 'qdrant'
            if a.startswith('aws_iam_role.reader'):
                if after['name']!='yeodam-v3-foundation-eso-'+n:raise ValueError('Wrong reader name')
                trust=json.loads(after['assume_role_policy'])
                if trust!={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':'arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-controller'},'Action':['sts:AssumeRole','sts:TagSession']}]}:raise ValueError('Only existing ESO controller trust')
            else:
                p=json.loads(after['policy'])
                if p!={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['secretsmanager:GetSecretValue','secretsmanager:DescribeSecret'],'Resource':['arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/'+n+'/*']}]}:raise ValueError('Wrong V3 secret prefix')
    return {'creates':4,'updates':1,'deletes':0}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);print(json.dumps(validate(json.loads(p.parse_args().plan.read_text()))))
