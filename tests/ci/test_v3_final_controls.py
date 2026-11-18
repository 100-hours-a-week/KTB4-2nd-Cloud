import copy,importlib.util,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'));import v3_final_controls as c

def waf():
 return {'Name':'yeodam-v3-validation','Id':c.WAF_ID,'Rules':[{'Name':'block-internal','Action':{'Block':{}},'Statement':{'OrStatement':{'Statements':[]}}}]+[{'Name':'observe-'+k,'Action':{'Count':{}},'Statement':{'RateBasedStatement':{'AggregateKeyType':'IP','EvaluationWindowSec':300,'Limit':limit,'ScopeDownStatement':{'fixture':k}}}}for k,limit in [('auth',500),('api',5000)]]}
class FinalControls(unittest.TestCase):
 def test_waf_keeps_windows_scope_and_thresholds_with_attributable_responses(self):
  original=waf();after=c.waf_target(original,True)
  self.assertEqual(original,waf())
  for old,new in zip(original['Rules'],after['Rules']):
   self.assertEqual(old['Statement'],new['Statement']);self.assertIn('CustomResponse',new['Action']['Block'])
   self.assertEqual(new['Action']['Block']['CustomResponse']['ResponseCode'],403 if old['Name']=='block-internal'else 429)
 def test_different_acl_forwarded_header_and_thresholds_rejected(self):
  for mode in ['id','header','threshold']:
   w=waf()
   if mode=='id':w['Id']='other'
   if mode=='header':w['Rules'][1]['Statement']['RateBasedStatement']['AggregateKeyType']='FORWARDED_IP'
   if mode=='threshold':w['Rules'][1]['Statement']['RateBasedStatement']['Limit']=50
   with self.assertRaises(ValueError):c.waf_target(w,True)
 def test_count_return_retains_internal_block(self):
  w=c.waf_target(c.waf_target(waf(),True),False)
  self.assertIn('Block',w['Rules'][0]['Action']);self.assertTrue(all(x['Action']=={'Count':{}}for x in w['Rules'][1:]))
 def test_uid_and_concurrent_spec_change_block_restore(self):
  from unittest.mock import patch
  p={'resource':'hpa','namespace':'trip','name':'trip-api','uid':'target','before':{'spec':{'maxReplicas':2},'annotations':{}},'after':{'spec':{'maxReplicas':1},'annotations':{}},'expiresAt':0}
  for live in [{'metadata':{'uid':'other'},'spec':{}},{'metadata':{'uid':'target','annotations':{}},'spec':{'maxReplicas':3}}]:
   with patch.object(c,'verify'),patch.object(c,'kube',return_value=live):
    with self.assertRaises(ValueError):c.profile_apply('synthetic',p,True)
if __name__=='__main__':unittest.main()
