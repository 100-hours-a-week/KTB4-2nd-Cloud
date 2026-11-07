#!/usr/bin/env python3
"""Accept initial isolated V3 ingress creates, or later validation DNS create. Never print a Plan."""
import argparse,json
from pathlib import Path
BASE={f'{kind}.{name}[0]' for kind,name in [('aws_iam_role','controller'),('aws_iam_role_policy','controller'),('aws_eks_pod_identity_association','controller'),('aws_security_group','alb'),('aws_acm_certificate','validation'),('aws_acm_certificate_validation','validation')]}
BASE|={f'aws_vpc_security_group_ingress_rule.public["{p}"]' for p in ['80','443']}
BASE|={f'{kind}.targets["{p}"]' for kind in ['aws_vpc_security_group_egress_rule','aws_vpc_security_group_ingress_rule'] for p in ['3000','8080','8081','9091']}
BASE.add('aws_route53_record.certificate["v3.yeodam-2gether.com"]')
DNS={'aws_route53_record.validation[0]'}
WAF={'aws_wafv2_web_acl.validation[0]','aws_wafv2_web_acl_association.validation[0]'}
def validate(plan):
    if not plan.get('complete'):raise ValueError('Complete Plan required')
    changed=[r for r in plan.get('resource_changes',[]) if r.get('mode')=='managed' and r['change']['actions']!=['no-op']]
    addresses={r['address'] for r in changed}
    if len(changed)!=len(addresses) or addresses not in [set(),BASE,WAF,DNS]:raise ValueError('Only initial17, WAF2 or later validation DNS1 creates allowed')
    for resource in changed:
        if resource['change']['actions']!=['create']:raise ValueError('Updates/deletes require separate review')
        value=resource['change']['after'];address=resource['address']
        if address=='aws_acm_certificate.validation[0]' and value.get('domain_name')!='v3.yeodam-2gether.com':raise ValueError('Validation host only')
        if address=='aws_security_group.alb[0]' and (value.get('vpc_id')!='vpc-0ecc3132f3b2170e9' or value.get('name')!='yeodam-v3-foundation-public-alb'):raise ValueError('V3 ALB SG only')
        if address=='aws_wafv2_web_acl.validation[0]' and (value.get('name')!='yeodam-v3-validation' or value.get('scope')!='REGIONAL'):raise ValueError('V3 regional WAF only')
        if address=='aws_wafv2_web_acl_association.validation[0]' and not value.get('resource_arn','').startswith('arn:aws:elasticloadbalancing:ap-northeast-2:483175530259:loadbalancer/app/yeodam-v3-validation/'):raise ValueError('Only the V3 ALB may receive WAF')
        if address in DNS and (value.get('name')!='v3.yeodam-2gether.com' or value.get('allow_overwrite') is not False):raise ValueError('Never overwrite production DNS')
    return {'creates':len(changed),'updates':0,'deletes':0,'scope':'V3 ingress only; no ALB controller reconciliation or live DNS health proof'}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);a=p.parse_args();print(json.dumps(validate(json.loads(a.plan.read_text()))))
if __name__=='__main__':main()
