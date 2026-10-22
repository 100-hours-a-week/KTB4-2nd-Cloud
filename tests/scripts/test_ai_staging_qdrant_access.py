import importlib.util
from pathlib import Path
import unittest

SPEC=importlib.util.spec_from_file_location('qdrant_access',Path(__file__).resolve().parents[2]/'scripts/configure-ai-staging-qdrant-access.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)

class PolicyExtensionTests(unittest.TestCase):
    def test_preserves_existing_policy_and_limits_added_scope(self):
        before={'Version':'2012-10-17','Statement':[{'Sid':'ExistingRedis','Effect':'Allow','Action':'ssm:StartSession','Resource':'existing-redis'}]}
        after=m.extend(before)
        self.assertEqual(before['Statement'],after['Statement'][:-1])
        added=after['Statement'][-1]
        self.assertEqual(added['Action'],'ssm:StartSession')
        self.assertEqual(added['Resource'],[m.TARGET_ARN,m.SHELL_DOCUMENT])
        self.assertEqual(len(before['Statement']),1)
        self.assertEqual(after,m.extend(after))

    def test_does_not_replace_a_conflicting_statement(self):
        with self.assertRaises(ValueError):
            m.extend({'Statement':[{'Sid':m.SID,'Action':'ssm:*','Resource':'*'}]})

if __name__=='__main__':unittest.main()
