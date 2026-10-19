"""Guard the production bootstrap recovery boundary and environment isolation."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class ProductionOutboxContract(unittest.TestCase):
    def test_pending_snapshot_and_insert_only_stream(self):
        c=json.loads((ROOT/'deploy/v2-production/kafka/outbox-connector.json').read_text())
        tables={f'yeodam.{x}' for x in ('trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox')}
        self.assertEqual(set(c['table.include.list'].split(',')),tables)
        self.assertEqual(c['snapshot.mode'],'initial')
        for table in tables:
            self.assertEqual(c['snapshot.select.statement.overrides.'+table],f"SELECT * FROM {table} WHERE cdc_managed = 1 AND status = 'PENDING'")
        condition=c['transforms.managedInsert.condition']
        self.assertIn("value.op == 'r'",condition)
        self.assertIn("value.op == 'c'",condition)
        self.assertIn("value.after.status == 'PENDING'",condition)
        self.assertNotIn("value.op == 'u'",condition)
        self.assertEqual(c['database.password'],'${file:/run/yeodam-cdc/mysql.properties:password}')
        self.assertEqual(c['database.hostname'],'mysql-predeploy.production.yeodam.internal')
        self.assertEqual(c['topic.prefix'],'prod.yeodam.outbox')

    def test_credential_scope_and_no_offset_reset(self):
        s=(ROOT/'terraform/v2/production/outbox-cdc.tf').read_text()
        self.assertNotIn('/staging/',s)
        self.assertIn('module.mysql.security_group_id',s)
        self.assertNotIn('cidr_ipv4',s)
        installer=(ROOT/'scripts/configure-v2-production-outbox-connector.py').read_text()
        self.assertIn("api('/connectors/'+NAME+'/config',config,'PUT')",installer)
        self.assertNotIn("'/offsets'",installer)
        self.assertIn("'--no-deps'",installer)
        self.assertNotIn("['down'",installer)

if __name__=='__main__':unittest.main()
