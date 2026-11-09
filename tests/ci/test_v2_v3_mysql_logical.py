import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('logical',ROOT/'scripts/v2_v3_mysql_logical.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class LogicalTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'backup';self.path.mkdir(mode=0o700)
  with gzip.open(self.path/'yeodam.sql.gz','wb') as f:f.write(b'CREATE TABLE synthetic(id INT PRIMARY KEY);')
  self.data={'formatVersion':1,'schema':'yeodam','sourceEngine':'9.7.2','sourceSchemaVersion':32,'sourceFrozen':True,'sensitivity':'synthetic','compressedBytes':(self.path/'yeodam.sql.gz').stat().st_size,'sha256':m.file_digest(self.path/'yeodam.sql.gz'),'sourceUuid':'fixture-source','tables':{}}
  self.save()
 def save(self):(self.path/'backup.json').write_text(json.dumps(self.data))
 def test_valid(self):self.assertEqual(m.verify_package(self.path),self.data)
 def test_tamper(self):
  with (self.path/'yeodam.sql.gz').open('ab') as f:f.write(b'tamper')
  with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_corrupt_gzip_even_if_hash_matches(self):
  f=self.path/'yeodam.sql.gz';f.write_bytes(b'notgzip');self.data.update(compressedBytes=7,sha256=m.file_digest(f));self.save()
  with self.assertRaises(gzip.BadGzipFile):m.verify_package(self.path)
 def test_private_rejected_in_ci(self):
  self.data['sensitivity']='private-user-data';self.save()
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}):
   with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_symlink_rejected(self):
  f=self.path/'yeodam.sql.gz';target=self.path.parent/'external';f.rename(target);f.symlink_to(target)
  with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_extra_files_rejected(self):
  (self.path/'secret').write_text('fixture')
  with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_unfrozen_rejected(self):
  self.data['sourceFrozen']=False;self.save()
  with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_wrong_engine_rejected(self):
  self.data['sourceEngine']='8.4.13';self.save()
  with self.assertRaises(ValueError):m.verify_package(self.path)
 def test_public_or_nonfixture_container_rejected_before_docker(self):
  with patch.object(m,'docker') as docker:
   with self.assertRaises(ValueError):m.isolated('yeodam-v2-mysql','source')
   docker.assert_not_called()
 def test_external_network_rejected(self):
  name='v2-v3-restore-0123456789-target'
  container=[{'State':{'Running':True},'Config':{'Image':m.TARGET_IMAGE,'Labels':{'yeodam.issue':'591'}},'HostConfig':{},'NetworkSettings':{'Networks':{name+'-net':{}}}}]
  with patch.object(m,'docker',side_effect=[json.dumps(container),json.dumps([{'Internal':False,'Labels':{'yeodam.issue':'591'}}])]):
   with self.assertRaises(ValueError):m.isolated(name,'target')
 def test_identifier_injection(self):
  for name in ['users; DROP DATABASE yeodam','`users`','mysql.users','users\n']:
   with self.assertRaises(ValueError):m.identifier(name)
 def test_empty_target_required_before_import(self):
  with patch.object(m,'isolated'),patch.object(m,'sql',side_effect=['8.4.13','different-uuid']),patch.object(m,'catalog',return_value=['users']),patch.object(m.subprocess,'Popen') as process:
   with self.assertRaises(ValueError):m.restore('fixture',self.path)
   process.assert_not_called()
 def test_dump_excludes_gtid_system_and_privileged_objects(self):
  self.assertIn('--set-gtid-purged=OFF',m.DUMP_OPTIONS);self.assertIn('--single-transaction',m.DUMP_OPTIONS)
  self.assertIn('--routines=false',m.DUMP_OPTIONS);self.assertNotIn('--all-databases',m.DUMP_OPTIONS)
if __name__=='__main__':unittest.main()
