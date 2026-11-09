import copy
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import v2_v3_photo_migration as p

KEY='trip-uploads/synthetic/original/photo.jpg'
def object_(version='source-version',body='synthetic',tags=None):
 return {'head':{'VersionId':version,'ContentLength':len(body),'ContentType':'image/jpeg',
  'Metadata':{'fixture':'only'},'ETag':'"'+hashlib.md5(body.encode()).hexdigest()+'"',
  'ServerSideEncryption':'AES256','ChecksumType':'FULL_OBJECT',
  'ChecksumSHA256':hashlib.sha256(body.encode()).hexdigest()},'tags':tags or {'status':'retained'}}
def refs(rows=None):
 return p.reference_manifest(rows or [{'key':KEY,'origin':'file.original','category':'required'}],'a'*64,'synthetic')
class Store:
 def __init__(self):self.source={KEY:object_()};self.target={};self.writes=[];self.race=False
 def assert_buckets(self):pass
 def observe(self,bucket,key,version=None):
  item=(self.source if bucket==p.SOURCE else self.target).get(key)
  if item is None:return None
  if version is not None and item['head']['VersionId']!=version:return None
  return copy.deepcopy(item)
 def copy_new(self,key,source):
  self.writes.append(key)
  if self.race:self.target[key]=object_('qa-version','QA changed');raise ValueError('precondition')
  if key in self.target:raise ValueError('precondition')
  self.target[key]=copy.deepcopy(source);self.target[key]['head']['VersionId']='copied-version'
  return {'CopySourceVersionId':source['head']['VersionId'],'VersionId':'copied-version'}
class PhotoTest(unittest.TestCase):
 def test_sql_integrity_and_read_only_shape(self):
  sql=p.SQL_PATH.read_bytes();self.assertEqual(hashlib.sha256(sql).hexdigest(),p.SQL_SHA256)
  self.assertTrue(sql.startswith(b'WITH '))
  for token in ['UPDATE ','DELETE ','INSERT ','DROP ','ALTER ','CREATE ']:self.assertNotIn(token,sql.decode().upper())
  for field in ['analyze_storage_key','preview_storage_key','display_storage_key','thumbnail_key','profile_image_url','initial_attachment_upload_items','additional_attachment_upload_items']:
   self.assertIn(field,sql.decode())
 def test_reference_dedup_and_required_wins(self):
  value=refs([{'key':KEY,'origin':'file.original','category':'required'},
   {'key':KEY,'origin':'trip.thumbnail','category':'required'},
   {'key':KEY,'origin':'initial-upload.original','category':'historical-upload'}])
  self.assertEqual(len(value['objects']),1);self.assertEqual(len(value['objects'][0]['origins']),2)
 def test_deleted_and_unfinished_not_copied(self):
  value=refs([{'key':KEY,'origin':'file.original','category':'deleted'},
   {'key':KEY,'origin':'initial-upload.original','category':'unfinished-upload'}])
  self.assertEqual(value['objects'],[]);self.assertEqual(sum(value['excludedReferences'].values()),2)
 def test_external_profile_not_requested(self):
  value=refs([{'key':'https://k.kakaocdn.net/fixture.jpg','origin':'user.profile','category':'required'}])
  self.assertEqual(value['objects'],[]);self.assertEqual(value['excludedReferences']['external-profile'],1)
 def test_http_kakao_profile_is_external_without_fetch(self):
  value=refs([{'key':'http://k.kakaocdn.net/fixture.jpg?fixture=1','origin':'user.profile','category':'required'}])
  self.assertEqual(value['objects'],[]);self.assertEqual(value['excludedReferences'],{'external-profile':1});self.assertFalse(value['blockedReferences'])
 def test_unknown_http_and_malformed_profile_still_block(self):
  for url in ['http://example.invalid/image.jpg','https:///missing-host.jpg']:
   self.assertTrue(refs([{'key':url,'origin':'user.profile','category':'required'}])['blockedReferences'])
 def test_deleted_story_attachment_not_reactivated(self):
  sql=p.SQL_PATH.read_text();live=sql.split('), refs AS')[0]
  self.assertIn('AND a.deleted_at IS NULL',live);self.assertNotIn('OR EXISTS',live)
 def test_absolute_source_profile_blocks(self):
  for url in ['https://'+p.SOURCE+'.s3-ap-northeast-2.amazonaws.com/'+KEY,
              'https://s3.ap-northeast-2.amazonaws.com/'+p.SOURCE+'/'+KEY,
              'https://s3-ap-northeast-2.amazonaws.com/'+p.SOURCE+'/'+KEY]:
   value=refs([{'key':url,'origin':'user.profile','category':'required'}]);self.assertTrue(value['blockedReferences'])
 def test_unknown_shared_and_path_keys_block(self):
  for key in ['ai-reference/places/index.sqlite','trip-downloads/file.zip','trip-uploads/../escape','trip-uploads/a/%2e%2e','trip-uploads/a//x','trip-uploads/a/line\nfeed']:
   self.assertTrue(refs([{'key':key,'origin':'file.original','category':'required'}])['blockedReferences'])
 def test_bad_file_state_blocks(self):
  value=refs([{'key':KEY,'origin':'attachment.original','category':'blocked-file-state'}])
  self.assertFalse(p.make_plan(value,Store())['ready'])
 def test_copy_then_rerun_reuses(self):
  store=Store();plan=p.make_plan(refs(),store)
  result=p.execute_plan(plan,store,p.digest(plan),1,len('synthetic'))
  self.assertTrue(result['completed']);self.assertEqual(result['counts'],{'copied':1})
  result=p.execute_plan(plan,store,p.digest(plan),1,len('synthetic'))
  self.assertEqual(result['counts'],{'reused':1});self.assertEqual(len(store.writes),1)
 def test_existing_same_object_reuse(self):
  store=Store();store.target[KEY]=object_('qa-existing')
  plan=p.make_plan(refs(),store);self.assertEqual(plan['copyCount'],0)
  result=p.execute_plan(plan,store,p.digest(plan),0,0)
  self.assertEqual(result['counts'],{'reused':1});self.assertFalse(store.writes)
 def test_existing_different_object_blocks_all_writes(self):
  store=Store();store.target[KEY]=object_('qa-existing','different')
  plan=p.make_plan(refs(),store);self.assertFalse(plan['ready'])
  with self.assertRaises(ValueError):p.execute_plan(plan,store,p.digest(plan),0,0)
  self.assertFalse(store.writes)
 def test_missing_object_blocks(self):
  store=Store();store.source={};plan=p.make_plan(refs(),store)
  self.assertFalse(plan['ready']);self.assertEqual(plan['counts'],{'missing-source':1})
 def test_metadata_and_tag_conflicts(self):
  for change in ['metadata','tag']:
   store=Store();store.target[KEY]=object_('qa')
   if change=='metadata':store.target[KEY]['head']['Metadata']={'different':'fixture'}
   else:store.target[KEY]['tags']={'status':'temporary'}
   self.assertFalse(p.make_plan(refs(),store)['ready'])
 def test_temporary_source_tag_blocks(self):
  store=Store();store.source[KEY]['tags']={'status':'temporary'}
  self.assertFalse(p.make_plan(refs(),store)['ready'])
 def test_multipart_etag_not_a_hash(self):
  source=object_()['head'];source.pop('ChecksumSHA256');source['ETag']='"'+'a'*32+'-2"'
  with self.assertRaises(ValueError):p.integrity(source)
 def test_single_sse_s3_md5_fallback(self):
  source=object_()['head'];source.pop('ChecksumSHA256')
  self.assertEqual(p.integrity(source)['method'],'etag-md5-sse-s3-single-part')
 def test_source_changed_before_copy_blocks_every_write(self):
  store=Store();plan=p.make_plan(refs(),store);store.source[KEY]=object_('new-version','changed')
  with self.assertRaises(ValueError):p.execute_plan(plan,store,p.digest(plan),plan['copyCount'],plan['copyBytes'])
  self.assertFalse(store.writes)
 def test_target_appearing_after_plan_blocks(self):
  store=Store();plan=p.make_plan(refs(),store);store.target[KEY]=object_('qa-new','changed')
  with self.assertRaises(ValueError):p.execute_plan(plan,store,p.digest(plan),1,plan['copyBytes'])
  self.assertFalse(store.writes)
 def test_destination_race_is_not_overwritten(self):
  store=Store();plan=p.make_plan(refs(),store);store.race=True
  result=p.execute_plan(plan,store,p.digest(plan),1,plan['copyBytes'])
  self.assertFalse(result['completed']);self.assertEqual(store.target[KEY]['head']['VersionId'],'qa-version')
 def test_plan_sha_count_and_bytes_must_match(self):
  store=Store();plan=p.make_plan(refs(),store)
  for sha,count,size in [('b'*64,1,plan['copyBytes']),(p.digest(plan),2,plan['copyBytes']),(p.digest(plan),1,plan['copyBytes']+1)]:
   with self.assertRaises(ValueError):p.execute_plan(plan,store,sha,count,size)
  self.assertFalse(store.writes)
 def test_expired_plan_refused(self):
  store=Store();plan=p.make_plan(refs(),store);plan['createdAt']=(p.now()-timedelta(hours=2)).isoformat()
  with self.assertRaises(ValueError):p.execute_plan(plan,store,p.digest(plan),1,plan['copyBytes'])
 def test_access_error_is_not_missing(self):
  store=Store()
  with patch.object(store,'observe',side_effect=RuntimeError('denied')):
   plan=p.make_plan(refs(),store)
  self.assertEqual(plan['counts'],{'inspection-failed':1});self.assertFalse(plan['ready'])
 def test_size_or_class_bound_blocks(self):
  for field,value in [('ContentLength',0),('ContentLength',5_000_000_001),('StorageClass','GLACIER')]:
   store=Store();store.source[KEY]['head'][field]=value
   self.assertFalse(p.make_plan(refs(),store)['ready'])
 def test_actual_cli_blocked_in_ci_without_sdk(self):
  spec=importlib.util.spec_from_file_location('cli',ROOT/'scripts/migrate-v2-photos.py')
  cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(sys,'argv',['tool','plan','--references','/unused','--output','/unused']):
   with self.assertRaises(ValueError):cli.main()

class SdkTest(unittest.TestCase):
 def test_destination_and_source_guards_in_real_sdk_model(self):
  try:import boto3;from botocore.stub import Stubber
  except ImportError:self.skipTest('Pinned SDK integration runs in schema CI venv')
  session=boto3.Session(aws_access_key_id='synthetic',aws_secret_access_key='synthetic',region_name='ap-northeast-2')
  store=p.S3Store(session);source=object_()
  params={'Bucket':p.TARGET,'Key':KEY,'CopySource':{'Bucket':p.SOURCE,'Key':KEY,'VersionId':'source-version'},
   'CopySourceIfMatch':source['head']['ETag'],'IfNoneMatch':'*','MetadataDirective':'COPY',
   'TaggingDirective':'REPLACE','Tagging':'status=retained','StorageClass':'STANDARD',
   'ServerSideEncryption':'AES256','ExpectedBucketOwner':p.ACCOUNT,'ExpectedSourceBucketOwner':p.ACCOUNT}
  with Stubber(store.client) as stub:
   stub.add_response('copy_object',{'VersionId':'new-version','CopySourceVersionId':'source-version'},params)
   self.assertEqual(store.copy_new(KEY,source)['VersionId'],'new-version');stub.assert_no_pending_responses()
 def test_403_head_is_never_treated_as_absent(self):
  try:import boto3;from botocore.stub import Stubber
  except ImportError:self.skipTest('Pinned SDK integration runs in schema CI venv')
  store=p.S3Store(boto3.Session(aws_access_key_id='synthetic',aws_secret_access_key='synthetic',region_name='ap-northeast-2'))
  with Stubber(store.client) as stub:
   stub.add_client_error('head_object','403',http_status_code=403)
   with self.assertRaises(RuntimeError):store.observe(p.TARGET,KEY)
if __name__=='__main__':unittest.main()
