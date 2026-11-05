import copy
import json
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('plan', ROOT / 'scripts/check-v3-karpenter-plan.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class KarpenterPlanTests(unittest.TestCase):
    def plan(self, action='create'):
        return {'resource_changes': [{'address': a, 'type': a.split('.')[0], 'mode': 'managed',
                'change': {'actions': [action]}} for a in sorted(p.EXPECTED)]}

    def test_fresh_and_idempotent_plan(self):
        self.assertEqual(p.validate(self.plan()), {'create': 22, 'no-op': 0})
        self.assertEqual(p.validate(self.plan('no-op')), {'create': 0, 'no-op': 22})

    def test_update_delete_replace_rejected(self):
        for actions in (['update'], ['delete'], ['delete', 'create'], ['read']):
            plan = self.plan(); plan['resource_changes'][0]['change']['actions'] = actions
            with self.subTest(actions=actions), self.assertRaises(ValueError): p.validate(plan)

    def test_ec2_nat_and_existing_roots_rejected(self):
        for address in ('aws_instance.unexpected', 'aws_nat_gateway.unexpected', 'aws_eks_node_group.system["a"]'):
            plan = self.plan(); plan['resource_changes'].append({'address': address})
            with self.subTest(address=address), self.assertRaises(ValueError): p.validate(plan)

    def test_missing_duplicate_wrong_type_errored_and_deferred_rejected(self):
        base = self.plan()
        cases = [copy.deepcopy(base) for _ in range(5)]
        cases[0]['resource_changes'].pop()
        cases[1]['resource_changes'].append(copy.deepcopy(base['resource_changes'][0]))
        cases[2]['resource_changes'][0]['type'] = 'aws_instance'
        cases[3]['errored'] = True
        cases[4]['deferred_changes'] = [{'reason': 'resource_config_unknown'}]
        for plan in cases:
            with self.assertRaises(ValueError): p.validate(plan)

    def credit_plan(self, action='create'):
        plan=self.plan('no-op')
        policy=json.loads((ROOT/'deploy/v3/nodepools/files/node-credit-policy.json').read_text())
        plan['resource_changes'].append({'address':'aws_iam_role_policy.node_credits[0]','type':'aws_iam_role_policy','mode':'managed',
            'change':{'actions':[action],'after':{'role':'yeodam-v3-foundation-karpenter-node','name':'v3-app-node-standard-credits','policy':json.dumps(policy)}}})
        return plan

    def test_optional_credit_policy_only(self):
        self.assertEqual(p.validate(self.credit_plan(),True),{'create':1,'no-op':22})
        self.assertEqual(p.validate(self.credit_plan('no-op'),True),{'create':0,'no-op':23})
        with self.assertRaises(ValueError):p.validate(self.credit_plan())

    def test_credit_optin_cannot_change_existing_or_wrong_role_scope(self):
        for kind in ('existing','role','action','tags','account-default'):
            plan=self.credit_plan();after=plan['resource_changes'][-1]['change']['after'];policy=json.loads(after['policy'])
            if kind=='existing':plan['resource_changes'][0]['change']['actions']=['create']
            if kind=='role':after['role']='yeodam-v3-foundation-system-node'
            if kind=='action':policy['Statement'][0]['Action']='ec2:*'
            if kind=='tags':policy['Statement'][0].pop('Condition')
            if kind=='account-default':plan['resource_changes'].append({'address':'aws_ec2_default_credit_specification.t3a'})
            after['policy']=json.dumps(policy)
            with self.subTest(kind=kind),self.assertRaises(ValueError):p.validate(plan,True)
