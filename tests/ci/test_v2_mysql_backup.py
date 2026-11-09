import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,ROOT/path);obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
export=module('export_v2','scripts/export-v2-mysql-backup.py')
restore=module('restore_v2','scripts/restore-v2-mysql-backup.py')
runner=module('run_v2','scripts/run-v2-mysql-backup.py')
RUN='20261109T100000Z-0123456789'
class BackupTest(unittest.TestCase):
 def test_wrong_host_refuses_before_docker(self):
  with patch.object(export.os,'geteuid',return_value=0),patch.dict(os.environ,{'GITHUB_ACTIONS':''}),patch.object(export,'identity_document',return_value={'accountId':export.ACCOUNT,'region':export.REGION,'instanceId':'i-wrong'}),patch.object(export.logical,'docker') as docker:
   with self.assertRaises(ValueError):export.assert_host()
   docker.assert_not_called()
 def test_ci_source_execution_forbidden(self):
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(export,'identity_document') as identity:
   with self.assertRaises(ValueError):export.assert_host()
   identity.assert_not_called()
 def test_bad_image_refused(self):
  with patch.object(export.os,'geteuid',return_value=0),patch.dict(os.environ,{'GITHUB_ACTIONS':''}),patch.object(export,'identity_document',return_value={'accountId':export.ACCOUNT,'region':export.REGION,'instanceId':export.INSTANCE}),patch.object(export.logical,'docker',return_value=json.dumps([{'State':{'Running':True},'Config':{'Image':'mysql:latest'}}])):
   with self.assertRaises(ValueError):export.assert_host()
 def test_history_checksum_refused(self):
  expected={i:{'script':str(i),'checksum':i} for i in range(1,33)}
  rows=[json.dumps({'version':str(i),'script':str(i),'checksum':i,'success':1}) for i in range(1,33)]
  with patch.object(export,'sql',return_value='\n'.join(rows)):export.validate_history(expected)
  rows[4]=json.dumps({'version':'5','script':'5','checksum':99,'success':1})
  with patch.object(export,'sql',return_value='\n'.join(rows)):
   with self.assertRaises(ValueError):export.validate_history(expected)
 def test_upload_create_only_encrypted(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'fixture';p.write_text('synthetic')
   head={'ServerSideEncryption':'AES256','ContentLength':9,'Metadata':{'sha256':export.logical.file_digest(p)},'VersionId':'version-one'}
   with patch.object(export.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0,'{}',''),subprocess.CompletedProcess([],0,json.dumps(head),'')]) as call:
    self.assertEqual(export.upload(p,'migration/v2-v3/'+RUN+'/backup.json'),'version-one')
    args=call.call_args_list[0].args[0]
    self.assertEqual(args[args.index('--bucket')+1],export.BUCKET)
    self.assertEqual(args[args.index('--if-none-match')+1],'*')
    self.assertEqual(args[args.index('--server-side-encryption')+1],'AES256')
 def test_download_forbidden_in_ci(self):
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(restore,'aws') as aws:
   with self.assertRaises(ValueError):restore.download(RUN,Path('/unused'),'profile')
   aws.assert_not_called()
 def test_invalid_run_command_refused(self):
  for value in ['../escape','x; rm -rf /',RUN+'\n']:
   with self.assertRaises(ValueError):runner.source_command(value,'a'*64)
 def test_source_command_exact_fixed_target(self):
  command=runner.source_command(RUN,'a'*64)
  self.assertIn(export.BUCKET,command);self.assertIn('--freeze-export-upload',command)
  self.assertNotIn('start-instances',command);self.assertNotIn('secretsmanager',command)
  self.assertIn('sha256sum -c -',command)
  self.assertTrue(command.startswith("/bin/bash <<'YEODAM_MIGRATION'"))
  self.assertTrue(command.endswith('YEODAM_MIGRATION'))
  subprocess.run(['bash','-n'],input=command,text=True,check=True)
 def test_schema_image_tamper_refused(self):
  with patch.object(restore.logical,'docker',side_effect=[json.dumps([{'Id':'sha256:'+'a'*64}]),'wrong hashes']):
   with self.assertRaises(ValueError):restore.schema_image_id('fixture')
 def test_private_package_rejected_before_docker_in_ci(self):
  with patch.object(restore.logical,'verify_package',side_effect=ValueError('private CI refused')),patch.object(restore,'schema_image_id') as image:
   with self.assertRaises(ValueError):restore.restore_package(Path('/unused'),'fixture',Path('/unused/runtime'))
   image.assert_not_called()
 def test_source_must_be_stopped_before_start(self):
  with tempfile.TemporaryDirectory() as tmp:
   def call(args,**kw):
    if 'get-caller-identity' in args:value={'Account':runner.ACCOUNT}
    elif 'describe-instances' in args:value={'Reservations':[{'Instances':[{'InstanceId':runner.INSTANCE,'InstanceType':'t4g.medium','State':{'Name':'running'}}]}]}
    else:raise AssertionError('Mutation attempted')
    return subprocess.CompletedProcess(args,0,json.dumps(value),'')
   with patch.dict(os.environ,{'GITHUB_ACTIONS':''}),patch.object(runner.subprocess,'run',side_effect=call):
    with self.assertRaises(ValueError):runner.execute(RUN,Path(tmp),'profile',Path(tmp)/'evidence')
 def test_source_stop_on_export_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   commands=[];started=False
   def call(args,**kw):
    nonlocal started
    commands.append(args)
    if 'get-caller-identity' in args:value={'Account':runner.ACCOUNT}
    elif 'describe-instances' in args:value={'Reservations':[{'Instances':[{'InstanceId':runner.INSTANCE,'InstanceType':'t4g.medium','State':{'Name':'stopped'}}]}]}
    elif 'list-services' in args:value={'serviceArns':['s1','s2','s3','s4']}
    elif 'describe-services' in args:value={'services':[{'desiredCount':0,'runningCount':0,'pendingCount':0}]*4}
    elif 'get-bucket-encryption' in args:value={'ServerSideEncryptionConfiguration':{'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}}
    elif 'get-bucket-versioning' in args:value={'Status':'Enabled'}
    elif 'put-object' in args:value={}
    elif 'start-instances' in args:started=True;value={}
    elif 'describe-instance-information' in args:value={'InstanceInformationList':[{'PingStatus':'Online'}]}
    elif 'send-command' in args:return subprocess.CompletedProcess(args,1,'','private error')
    elif 'stop-instances' in args:self.assertTrue(started);value={}
    else:raise AssertionError(args)
    return subprocess.CompletedProcess(args,0,json.dumps(value),'')
   with patch.dict(os.environ,{'GITHUB_ACTIONS':''}),patch.object(runner,'build_bundle',return_value=b'public code'),patch.object(runner.subprocess,'run',side_effect=call):
    with self.assertRaises(ValueError):runner.execute(RUN,Path(tmp),'profile',Path(tmp)/'evidence')
   self.assertTrue(any('stop-instances' in a for a in commands))
   for args in commands:
    if 'start-instances' in args or 'stop-instances' in args:self.assertEqual(args[-1],runner.INSTANCE)
   self.assertTrue((Path(tmp)/'evidence/source-stopped.json').exists())
 def test_live_target_wrong_label_refused(self):
  name='v2-v3-live-restore-0123456789-target'
  value=[{'State':{'Running':True},'Config':{'Image':restore.logical.TARGET_IMAGE,'Labels':{'yeodam.issue':'591'}},'HostConfig':{},'NetworkSettings':{}}]
  with patch.object(restore.logical,'docker',return_value=json.dumps(value)):
   with self.assertRaises(ValueError):restore.logical.isolated(name,'target')
 def test_capture_shared_callback_hashes_rows(self):
  shape={'users':{'columns':['user_id'],'primaryKey':['user_id']}}
  with patch.object(export.logical,'sql') as original:
   result=export.logical.capture(None,shape,query_fn=lambda q:'1' if 'COUNT(*)' in q else 'V31')
   original.assert_not_called()
   self.assertEqual(result['users']['rows'],1)
if __name__=='__main__':unittest.main()
