#!/usr/bin/env python3
"""Validate the exact additive private embedding path; never print a plan or use AWS."""
import argparse,json
from pathlib import Path

V3_ROUTES=['rtb-036f77e913361faa0','rtb-0ce761884715b3a42','rtb-04c95e08b723549a4']
def expected():
    resources={'aws_vpc_peering_connection.embedding[0]':{'vpc_id':'vpc-0ecc3132f3b2170e9','peer_vpc_id':'vpc-0d84786e134e32933','auto_accept':True},
      'aws_route.to_v3[0]':{'route_table_id':'rtb-0fc40e73348be69f0','destination_cidr_block':'10.40.0.0/16'},
      'aws_vpc_security_group_ingress_rule.embedding[0]':{'security_group_id':'sg-0a80da95d51c138fe','referenced_security_group_id':'sg-068c6d24435af33fb','from_port':8000,'to_port':8000,'ip_protocol':'tcp'},
      'aws_route53_zone_association.embedding[0]':{'zone_id':'Z0848608U1XFD63QO507','vpc_id':'vpc-0ecc3132f3b2170e9','vpc_region':'ap-northeast-2'}}
    resources.update({'aws_route.to_embedding["'+r+'"]':{'route_table_id':r,'destination_cidr_block':'10.30.10.162/32'}for r in V3_ROUTES})
    return resources

def validate(plan,enabled=True):
    changes=[c for c in plan.get('resource_changes',[])if c.get('mode','managed')=='managed' and c['change']['actions']!=['no-op']]
    targets=expected() if enabled else {}
    if len(changes)!=len(targets) or {c['address']for c in changes}!=set(targets):raise ValueError('Only the exact seven private path resources may be created')
    for c in changes:
        change=c['change'];after=change['after']
        if change['actions']!=['create'] or change.get('before')is not None:raise ValueError('No updates, replacements or deletion of existing resources')
        if not all(after.get(k)==v for k,v in targets[c['address']].items()):raise ValueError('Unexpected path target or access scope')
        if c['address'].startswith('aws_route.'):
            if not change.get('after_unknown',{}).get('vpc_peering_connection_id') or after.get('gateway_id') or after.get('nat_gateway_id'):raise ValueError('Route must use the newly created peer only')
        if c['address'].startswith('aws_vpc_security_group_ingress_rule.') and any(after.get(k) for k in ['cidr_ipv4','cidr_ipv6','prefix_list_id']):raise ValueError('Only the V3 node SG may access TCP8000')
    return {'creates':len(changes),'updates':0,'deletes':0,'instanceChanges':0,'gpuConfigChanges':0}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('plan',type=Path);parser.add_argument('--disabled',action='store_true');args=parser.parse_args()
    print(json.dumps(validate(json.loads(args.plan.read_text()),not args.disabled)))
if __name__=='__main__':main()
