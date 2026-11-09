"""Migration evidence safety, schema drift and count reconciliation without AWS or user data."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import v2_v3_migration_contract as contract

NOW = datetime(2026,11,9,4,0,0,tzinfo=timezone.utc)
TIME = '2026-11-09T04:00:00Z'
EXPECTED = {i: {'script':f'V{i}__fixture.sql','checksum':i*17} for i in range(1,36)}


def evidence(version=35, uuid='11111111-1111-1111-1111-111111111111'):
    return [{'kind':'meta','schema':'yeodam','engine':'9.7.2','serverUuid':uuid,'capturedAt':TIME,'readOnly':0},
            *[{'kind':'history','rank':i,'version':str(i),**EXPECTED[i],'success':1} for i in range(1,version+1)],
            *[{'kind':'table','table':t,'estimatedRows':4,'dataBytes':100,'indexBytes':20} for t in ['flyway_schema_history','trips']],
            *[{'kind':'column','table':t,'column':'id','position':1,'type':'bigint unsigned','nullable':'NO','collation':None} for t in ['flyway_schema_history','trips']]]


def inventory():
    result=contract.template();result.update(capturedAt=TIME,sourceWritersFrozen=True,sourceActiveTransactions=0,
        mysqlPendingJobs=0,qaDataPolicy='archive-before-replace',cdcRestartPlanReviewed=True,rollbackAfterNewWritesReviewed=True)
    for name in contract.STRATEGIES:
        result['stores'][name]={'strategy':sorted(contract.STRATEGIES[name])[0],'referencesReviewed':True,'pendingItems':0}
    return result


class Migration(unittest.TestCase):
    def check(self, source=None, target=None, inv=None):
        return contract.preflight(source if source is not None else evidence(32),
            target if target is not None else evidence(uuid='22222222-2222-2222-2222-222222222222'),
            inv if inv is not None else inventory(), EXPECTED,NOW)

    def test_success_still_never_authorizes_migration(self):
        r=self.check();self.assertTrue(r['metadataChecksPassed']);self.assertFalse(r['readyToMigrate'])
        self.assertEqual(r['sourceSchemaVersion'],32);self.assertEqual(r['pendingMigrationVersions'],[33,34,35])
        self.assertFalse(r['resourcesChanged']);self.assertNotIn('serverUuid',json.dumps(r))

    def test_numeric_history_not_string_max(self):
        self.assertEqual(contract.schema(evidence(10),EXPECTED,NOW)['latestVersion'],10)
        self.assertEqual(contract.schema(evidence(35),EXPECTED,NOW)['latestVersion'],35)

    def test_failed_missing_reordered_checksum_or_unexpected_history_blocks(self):
        for mutation in ['failed','gap','order','checksum','script','duplicate-rank','baseline']:
            rows=evidence(32)
            if mutation=='failed':rows[1]['success']=0
            if mutation=='gap':rows.pop(9)
            if mutation=='order':rows[9]['rank'],rows[10]['rank']=rows[10]['rank'],rows[9]['rank']
            if mutation=='checksum':rows[1]['checksum']+=1
            if mutation=='script':rows[1]['script']='V1__changed.sql'
            if mutation=='duplicate-rank':rows[2]['rank']=1
            if mutation=='baseline':rows[1]['version']=None
            with self.subTest(mutation=mutation):self.assertIn('source_mysql_evidence_invalid',self.check(source=rows)['blockers'])

    def test_stale_future_same_server_engine_and_target_version(self):
        for stamp in ['2026-11-09T02:59:59Z','2026-11-09T04:00:01Z']:
            rows=evidence();rows[0]['capturedAt']=stamp
            self.assertIn('source_mysql_evidence_invalid',self.check(source=rows)['blockers'])
        self.assertIn('source_target_are_same_server',self.check(target=evidence())['blockers'])
        dst=evidence(uuid='22222222-2222-2222-2222-222222222222');dst[0]['engine']='9.6.0'
        self.assertIn('engine_difference_requires_restore_rehearsal',self.check(target=dst)['blockers'])
        self.assertIn('target_not_pinned_v35',self.check(target=evidence(34))['blockers'])

    def test_same_version_structural_drift_is_not_hidden_by_flyway_history(self):
        target=evidence(uuid='22222222-2222-2222-2222-222222222222')
        next(r for r in target if r['kind']=='column')['type']='varchar(255)'
        self.assertIn('same_version_schema_drift',self.check(source=evidence(),target=target)['blockers'])

    def test_missing_evidence_and_template_fail_closed(self):
        r=contract.preflight(None,None,contract.template(),EXPECTED,NOW)
        self.assertFalse(r['metadataChecksPassed']);self.assertIn('source_mysql_evidence_missing',r['blockers'])
        self.assertIn('source_not_frozen',r['blockers']);self.assertIn('qa_data_disposition_missing',r['blockers'])

    def test_every_store_scope_pending_jobs_and_controls(self):
        for field,value in [('bindings',{}),('sourceWritersFrozen',False),('sourceActiveTransactions',1),
             ('mysqlPendingJobs',1),('qaDataPolicy','pending'),('cdcRestartPlanReviewed',False),('rollbackAfterNewWritesReviewed',False)]:
            inv=inventory();inv[field]=value
            with self.subTest(field=field):self.assertFalse(self.check(inv=inv)['metadataChecksPassed'])
        for name in contract.STRATEGIES:
            for field,value in [('strategy','copy-offsets'),('referencesReviewed',False),('pendingItems',1),('pendingItems',True)]:
                inv=inventory();inv['stores'][name][field]=value
                with self.subTest(store=name,field=field):self.assertFalse(self.check(inv=inv)['metadataChecksPassed'])

    def test_unknown_inventory_fields_and_incomplete_catalog(self):
        inv=inventory();inv['password']='synthetic-do-not-echo'
        r=self.check(inv=inv);self.assertFalse(r['metadataChecksPassed']);self.assertNotIn('synthetic-do-not-echo',json.dumps(r))
        rows=evidence();rows.pop();self.assertIn('source_mysql_evidence_invalid',self.check(source=rows)['blockers'])

    def test_flyway_signed_crc_and_line_endings(self):
        raw=b'SELECT 1;\nSELECT 2;\n';want=zlib.crc32(b'SELECT 1;SELECT 2;');want=want if want<2**31 else want-2**32
        self.assertEqual(contract.flyway_crc(raw),want)
        self.assertEqual(contract.flyway_crc(b'\xef\xbb\xbf'+raw.replace(b'\n',b'\r\n')),want)
        self.assertNotEqual(contract.flyway_crc(raw+b'--change'),want)

    def test_pinned_sql_hash_set_and_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);raw=b'SELECT 1;\n';f=p/'V1__fixture.sql';f.write_bytes(raw)
            lock={'schemaVersion':1,'migrations':[{'name':f.name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}
            self.assertEqual(contract.sql_checksums(p,lock)[1]['checksum'],contract.flyway_crc(raw))
            f.write_bytes(b'ALTER TABLE secret;')
            with self.assertRaises(ValueError):contract.sql_checksums(p,lock)
            f.unlink();f.symlink_to(ROOT/'scripts/cloud-pr-scope.py')
            with self.assertRaises(ValueError):contract.sql_checksums(p,lock)

    def test_count_comparison_full_coverage_and_content_limit(self):
        before=[{'kind':'count','table':'trips','rows':8},{'kind':'count','table':'flyway_schema_history','rows':32}]
        after=[{'kind':'count','table':'trips','rows':8},{'kind':'count','table':'flyway_schema_history','rows':35},{'kind':'count','table':'new_table','rows':0}]
        def run(a=after):return contract.compare_counts(before,a,{'trips','flyway_schema_history'},{'trips','flyway_schema_history','new_table'})
        r=run();self.assertTrue(r['rowCountsMatch']);self.assertFalse(r['rowContentsVerified']);self.assertFalse(r['readyToMigrate'])
        bad=deepcopy(after);bad[0]['rows']=7;self.assertFalse(run(bad)['rowCountsMatch'])
        bad=deepcopy(after);bad[2]['rows']=1;self.assertFalse(run(bad)['rowCountsMatch'])
        with self.assertRaises(ValueError):run(after[:-1])
        with self.assertRaises(ValueError):run(after+[after[0]])

    def test_counts_sql_rejects_injection_and_only_selects_business_counts(self):
        sql=contract.counts_sql(evidence());self.assertIn('COUNT(*)',sql);self.assertNotIn('INSERT',sql)
        rows=evidence();next(r for r in rows if r['kind']=='table')['table']='trips`; DROP TABLE users;--'
        with self.assertRaises(ValueError):contract.counts_sql(rows)

    def test_cli_private_templates_exclusive_no_secret_echo(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'evidence';cmd=[sys.executable,str(ROOT/'scripts/check-v2-v3-migration.py'),'init','--directory',str(p)]
            subprocess.run(cmd,check=True,capture_output=True)
            self.assertEqual(p.stat().st_mode & 0o777,0o700);self.assertEqual((p/'inventory.json').stat().st_mode & 0o777,0o600)
            sentinel=p/'inventory.json';sentinel.write_text('synthetic-token-do-not-echo')
            result=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(result.returncode,1)
            self.assertNotIn('synthetic-token',result.stdout);self.assertEqual(sentinel.read_text(),'synthetic-token-do-not-echo')

    def test_jsonl_unknown_fields_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'metadata.jsonl';p.write_text(json.dumps({'kind':'meta','password':'synthetic'}))
            with self.assertRaises(ValueError):contract.load_records(p)
            p.unlink();p.symlink_to(ROOT/'scripts/cloud-pr-scope.py')
            with self.assertRaises(ValueError):contract.load_records(p)

    def test_repository_public_bindings_match_current_deployment(self):
        self.assertEqual(contract.repository_bindings(),contract.BINDINGS)

    def test_inventory_wrong_types_and_stale_time_fail_closed(self):
        for inv in [[], 'synthetic-do-not-echo']:
            r=contract.preflight(evidence(),evidence(),inv,EXPECTED,NOW)
            self.assertFalse(r['metadataChecksPassed']);self.assertNotIn('synthetic',json.dumps(r))
        for field,value in [('stores',[]),('capturedAt','2026-11-09T02:59:59Z')]:
            inv=inventory();inv[field]=value;self.assertFalse(self.check(inv=inv)['metadataChecksPassed'])
        inv=inventory();inv['stores']['s3']=None;self.assertFalse(self.check(inv=inv)['metadataChecksPassed'])

    def test_cli_only_local_io_no_resource_clients(self):
        import ast
        for name in ['scripts/check-v2-v3-migration.py','scripts/v2_v3_migration_contract.py']:
            tree=ast.parse((ROOT/name).read_text())
            modules=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            modules += [a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
            self.assertFalse(set(modules)&{'subprocess','boto3','urllib','requests','socket','pymysql'})

    def test_metadata_sql_does_not_read_business_rows(self):
        sql=(ROOT/'scripts/migration/mysql-metadata.sql').read_text()
        self.assertIn('START TRANSACTION READ ONLY',sql)
        self.assertIn('ORDER BY installed_rank',sql)
        self.assertNotIn('MAX(version)',sql)
        self.assertNotIn('FROM yeodam.trips',sql)
        self.assertNotIn('SELECT *',sql)


if __name__=='__main__':unittest.main()
