import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('read406',ROOT/'scripts/analyze-b406-reads.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class ReadAnalysis(unittest.TestCase):
    def test_http_failure_and_invalid_body_remain_failures(self):
        rows=[{'event':'read_response','kind':'list','at':100001+i,'elapsedMs':v,'status':s,'ok':ok,'connectingMs':0,'secret':'must not copy'} for i,(v,s,ok) in enumerate([(10,200,True),(1000,503,False),(20,200,False)])]
        value=m.analyze(rows,100)
        self.assertEqual(value['all']['list']['failures'],2)
        self.assertEqual(value['all']['list']['count'],3)
        self.assertNotIn('secret',str(value))
        self.assertEqual(value['all']['detail']['latencyMs'],{})
    def test_boundaries_do_not_overlap(self):
        rows=[{'event':'read_response','kind':'detail','at':s*1000,'elapsedMs':10,'status':200,'ok':True,'connectingMs':0} for s in (101,120,130)]
        value=m.analyze(rows,100,120,130)
        self.assertEqual([value[k]['detail']['count'] for k in ('beforeFence','duringFence','afterEnable')],[1,1,1])
if __name__=='__main__':unittest.main()
