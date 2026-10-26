import asyncio
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('probe436',Path(__file__).parents[1]/'scripts/nginx-tcp-capacity.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
class WireTest(unittest.TestCase):
 def test_recovery_uses_valid_http_framing(self):
  request=probe.wire_request(close=True)
  self.assertEqual(request.count(b'\r\n'),5)
  self.assertTrue(request.endswith(b'Connection: close\r\n\r\n'))
  self.assertNotIn(b'\\r\\n',request)
 def test_truncated_chunked_response_fails_instead_of_spinning(self):
  async def exercise():
   reader=asyncio.StreamReader();reader.feed_data(b'HTTP/1.1 401 Unauthorized\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n');reader.feed_eof()
   with self.assertRaises(ValueError):await asyncio.wait_for(probe.response(reader),.1)
  asyncio.run(exercise())
if __name__=='__main__':unittest.main()
