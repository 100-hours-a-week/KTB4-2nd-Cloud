"""Retry transport failures without weakening integrity or authorization failures."""
import hashlib,importlib.util,io,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('download',ROOT/'scripts/prepare-v3-validation.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Download(unittest.TestCase):
 def entry(self):return {'url':'https://official.example/pinned.tgz','sha256':hashlib.sha256(b'verified').hexdigest()}
 def test_transient_504_then_verified_success(self):
  error=urllib.error.HTTPError(self.entry()['url'],504,'Gateway timeout',{},None)
  with patch.object(m.urllib.request,'urlopen',side_effect=[error,io.BytesIO(b'verified')]) as request,patch.object(m.time,'sleep'):
   self.assertEqual(m.download(self.entry()),b'verified');self.assertEqual(request.call_count,2)
 def test_release_asset_accept_keeps_checksum_verification(self):
  entry={**self.entry(),'accept':'application/octet-stream'}
  with patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'verified')) as request:
   self.assertEqual(m.download(entry),b'verified')
   self.assertEqual(request.call_args.args[0].get_header('Accept'),'application/octet-stream')
  with patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'tampered')):
   with self.assertRaisesRegex(ValueError,'checksum'):m.download(entry)
 def test_forbidden_is_not_retried(self):
  error=urllib.error.HTTPError(self.entry()['url'],403,'Forbidden',{},None)
  with patch.object(m.urllib.request,'urlopen',side_effect=error) as request,patch.object(m.time,'sleep'):
   with self.assertRaises(urllib.error.HTTPError):m.download(self.entry())
   self.assertEqual(request.call_count,1)
 def test_checksum_failure_is_not_retried(self):
  with patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'tampered')) as request,patch.object(m.time,'sleep'):
   with self.assertRaisesRegex(ValueError,'checksum'):m.download(self.entry())
   self.assertEqual(request.call_count,1)
 def test_retry_count_is_bounded(self):
  error=urllib.error.HTTPError(self.entry()['url'],504,'Gateway timeout',{},None)
  with patch.object(m.urllib.request,'urlopen',side_effect=error) as request,patch.object(m.time,'sleep'):
   with self.assertRaises(urllib.error.HTTPError):m.download(self.entry())
   self.assertEqual(request.call_count,3)
if __name__=='__main__':unittest.main()
