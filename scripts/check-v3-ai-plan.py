#!/usr/bin/env python3
"""Allow initial AI identities or one exact TourAPI read-policy update; never print the Plan."""
import argparse,json
from pathlib import Path
EXPECTED={f'{t}.app["{r}"]' for t in ['aws_iam_role','aws_eks_pod_identity_association'] for r in ['ai-api','ai-runner']}|{f'aws_iam_role_policy.read["{r}"]' for r in ['ai-api','ai-runner']}
TOURAPI='arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/tourapi/v2026-11-11/tourapi_all.json'

def validate(plan):
    if not plan.get('complete'):raise ValueError('Complete Plan required')
    changes=[r for r in plan.get('resource_changes',[]) if r.get('mode')=='managed' and r['change']['actions']!=['no-op']]
    if not changes:return {'creates':0,'updates':0,'deletes':0}
    if len(changes)==1 and changes[0]['address']=='aws_iam_role_policy.read["ai-runner"]':
        change=changes[0]['change']
        if change['actions']!=['update']:raise ValueError('Only runner policy update allowed')
        before,after=change['before'],change['after']
        if {k:v for k,v in before.items() if k!='policy'}!={k:v for k,v in after.items() if k!='policy'}:raise ValueError('Policy identity must not change')
        old=json.loads(before['policy']);new=json.loads(after['policy'])
        expected=json.loads(json.dumps(old))
        resources=expected['Statement'][0]['Resource']
        baseline=['arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-uploads/*/derived/*','arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-additions/*/derived/*','arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite']
        if resources!=baseline:raise ValueError('Existing runner scope differs from the reviewed baseline')
        if TOURAPI in resources:raise ValueError('Not a TourAPI-only addition')
        resources.append(TOURAPI)
        if new!=expected or old.get('Version')!='2012-10-17' or len(old['Statement'])!=1 or old['Statement'][0]['Action']!=['s3:GetObject'] or old['Statement'][0]['Effect']!='Allow':raise ValueError('Only exact read-only TourAPI object addition allowed')
        return {'creates':0,'updates':1,'deletes':0}
    if len(changes)!=6 or {r['address'] for r in changes}!=EXPECTED:raise ValueError('Only six AI identity creates allowed')
    for r in changes:
        c=r['change'];a=c['after'];role='ai-runner' if 'ai-runner' in r['address'] else 'ai-api'
        if c['actions']!=['create']:raise ValueError('No update/delete')
        if r['type']=='aws_iam_role':
            if a['name']!='yeodam-v3-foundation-'+role:raise ValueError('Wrong AI role')
            document=json.loads(a['assume_role_policy'])
            if document.get('Version')!='2012-10-17' or len(document.get('Statement',[]))!=1:raise ValueError('One exact trust statement only')
            trust=document['Statement'][0]
            if trust.get('Effect')!='Allow':raise ValueError('Exact allow trust required')
            if trust.get('Principal')!={'Service':'pods.eks.amazonaws.com'} or set(trust.get('Action',[]))!={'sts:AssumeRole','sts:TagSession'} or trust['Condition']['StringEquals']!={'aws:SourceAccount':'483175530259','aws:RequestTag/kubernetes-namespace':'ai','aws:RequestTag/kubernetes-service-account':role} or trust['Condition']['ArnEquals']!={'aws:SourceArn':'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'}:raise ValueError('Exact Pod Identity trust only')
        elif r['type']=='aws_eks_pod_identity_association':
            if a['cluster_name']!='yeodam-v3-foundation' or a['namespace']!='ai' or a['service_account']!=role:raise ValueError('Exact AI association only')
        else:
            statements=json.loads(a['policy'])['Statement'];expected=['arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-uploads/*/derived/*','arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-additions/*/derived/*']
            if role=='ai-runner':expected+=['arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite',TOURAPI]
            if statements!=[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':expected}]:raise ValueError('Read-only exact S3 object scope')
    return {'creates':6,'updates':0,'deletes':0}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);print(json.dumps(validate(json.loads(p.parse_args().plan.read_text()))))
