"""Check meaningful collection and authority boundaries, not application behavior."""
import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('ai_logs',ROOT/'scripts/check-v3-ai-logs.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.docs=m.render(True)
    def test_disabled_and_enabled(self):
        self.assertEqual(m.render(False),[]);self.assertTrue(m.validate(self.docs))
    def test_expanded_source_or_privileges_rejected(self):
        for mutate in [lambda d:d.append({'kind':'ClusterRole'}),lambda d:next(x for x in d if x['kind']=='DaemonSet')['spec']['template']['spec'].update(hostNetwork=True),lambda d:next(x for x in d if x['kind']=='ConfigMap')['data'].update({'fluent-bit.conf':'Path /var/log/containers/*.log'})]:
            docs=copy.deepcopy(self.docs);mutate(docs)
            with self.assertRaises(ValueError):m.validate(docs)
    def test_node_placement_or_resource_growth_rejected(self):
        for field in ['affinity','resources']:
            docs=copy.deepcopy(self.docs);pod=next(x for x in docs if x['kind']=='DaemonSet')['spec']['template']['spec']
            if field=='affinity':pod[field]={}
            else:pod['containers'][0][field]['limits']['memory']='1Gi'
            with self.assertRaises((ValueError,KeyError)):m.validate(docs)
if __name__=='__main__':unittest.main()
