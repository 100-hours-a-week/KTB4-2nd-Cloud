"""Exact V40 migration effects and approved image guard; synthetic metadata only."""
import copy,hashlib,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v2_v3_mysql_logical as logical
import v2_v3_cutover as cutover

class Upgrade(unittest.TestCase):
 def fixture(self):
  tables={'additional_attachment_executions':{'columns':['id','user_id'],'primaryKey':['id'],'rows':1,'sha256':'fixed'}}
  baseline={'tables':tables,'startedAt':'2026-11-10 00:00:00.000000','historySha256':hashlib.sha256(b'history').hexdigest()}
  def execute(query):
   if query=='SELECT NOW(6);':return '2026-11-10 00:01:00.000000'
   if 'SELECT * FROM yeodam.flyway_schema_history' in query:return 'history'
   if 'COUNT(DISTINCT version)' in query:return '40:40:40:40'
   if 'table_name FROM' in query:return '\n'.join(set(tables)|logical.UPGRADE_TABLES)
   return '0'
  return baseline,execute
 def test_exact_effects_pass(self):
  baseline,execute=self.fixture()
  with patch.object(logical,'capture',return_value=baseline['tables']):self.assertTrue(logical.verify_upgrade(execute,baseline)['reviewedTimestampOnly'])
 def test_wrong_actor_protection_time_and_new_rows_block(self):
  for key in ['accepted_actor_id','requires_remote_termination_confirmation','`trip_analysis_executions`']:
   baseline,execute=self.fixture()
   def mutated(query):return '1' if key in query else execute(query)
   with self.subTest(key=key),patch.object(logical,'capture',return_value=baseline['tables']),self.assertRaises(ValueError):logical.verify_upgrade(mutated,baseline)
 def test_unreviewed_legacy_change_blocks(self):
  baseline,execute=self.fixture()
  with patch.object(logical,'capture',return_value={}),self.assertRaises(ValueError):logical.verify_upgrade(execute,baseline)
 def test_history_tamper_or_missing_migration_blocks(self):
  for key in ['SELECT * FROM','COUNT(DISTINCT version)']:
   baseline,execute=self.fixture()
   with patch.object(logical,'capture',return_value=baseline['tables']),self.assertRaises(ValueError):logical.verify_upgrade(lambda q:'wrong' if key in q else execute(q),baseline)
 def test_only_reviewed_timestamp_removed(self):
  shape={'additional_attachment_executions':{'columns':['id','updated_at'],'primaryKey':['id']},'users':{'columns':['id','updated_at'],'primaryKey':['id']}}
  def execute(q):return '2026-11-10 00:00:00.000000' if q=='SELECT NOW(6);' else 'history'
  with patch.object(logical,'capture',return_value={}) as capture:logical.upgrade_baseline(execute,shape)
  passed=capture.call_args.args[1]
  self.assertEqual(passed['additional_attachment_executions']['columns'],['id']);self.assertEqual(passed['users']['columns'],['id','updated_at']);self.assertIn('updated_at',shape['additional_attachment_executions']['columns'])
 def test_retired_v35_and_tags_refused(self):
  for ref in [cutover.POLICY['retiredSchemaImage'],'ghcr.io/100-hours-a-week/yeodam-v3-mysql-schema:latest']:
   with self.assertRaises(ValueError):cutover.require_schema_image(ref)
  cutover.require_schema_image('ghcr.io/100-hours-a-week/yeodam-v3-mysql-schema@sha256:'+'a'*64)
 def test_reviewed_sql_matches_frozen_v35_prefix(self):
  source=logical.SCHEMA_LOCK;baseline=json.loads((ROOT/'deploy/v3/mysql-schema-image/v35-baseline.lock.json').read_text())
  self.assertEqual(source['migrations'][:35],baseline['migrations']);self.assertEqual(len(source['migrations']),40)

if __name__=='__main__':unittest.main()
