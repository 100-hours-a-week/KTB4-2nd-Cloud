import asyncio
import importlib.util
from pathlib import Path
import ssl
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('tls440', Path(__file__).parents[1]/'scripts/nginx-http-tls-load.py')
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


class IsolationTest(unittest.TestCase):
    def test_staging_and_policy_bounds(self):
        a = SimpleNamespace(mode='fresh', confirm_target=load.ORIGIN, rate=21, seconds=30, workers=4)
        with self.assertRaises(ValueError): load.validate(a)
        a.rate = 20
        load.validate(a)
        a.confirm_target = 'https://yeodam-2gether.com'
        with self.assertRaises(ValueError): load.validate(a)

    def test_no_samples_are_not_zero_latency(self):
        self.assertIsNone(load.duration_summary([]))
        self.assertEqual(load.duration_summary([10, 20])['p95'], 20)

    def test_real_local_tls_reuse_and_full_handshakes(self):
        # Real loopback TLS checks connection reuse rather than mirroring a counter formula.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert, key = root/'cert.pem', root/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           check=True, capture_output=True)
            factory = ssl.create_default_context

            async def check():
                server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server_context.load_cert_chain(cert, key)
                server_context.set_alpn_protocols(['http/1.1'])
                seen = []

                async def client(reader, writer):
                    seen.append(writer.get_extra_info('ssl_object').version())
                    try:
                        while True:
                            header = await reader.readuntil(b'\r\n\r\n')
                            close = b'Connection: close' in header
                            writer.write(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: '+
                                         (b'close' if close else b'keep-alive')+b'\r\n\r\n')
                            await writer.drain()
                            if close: break
                    except (asyncio.IncompleteReadError, ConnectionError):
                        pass
                    finally:
                        writer.close()

                server = await asyncio.start_server(client, '127.0.0.1', 0, ssl=server_context)
                port = server.sockets[0].getsockname()[1]
                original = asyncio.open_connection

                async def local_connection(address, ignored):
                    return await original('127.0.0.1', port)

                try:
                    results = []
                    with patch.object(load, 'HOST', 'localhost'), patch.object(load.ssl, 'create_default_context',
                        side_effect=lambda: factory(cafile=str(cert))), patch.object(load.asyncio, 'open_connection', local_connection):
                        for mode in ['reuse', 'fresh']:
                            a = SimpleNamespace(mode=mode, rate=4, seconds=1, workers=2, stop_file=None)
                            results.append(await load.exercise(a))
                    reuse, fresh = results
                    self.assertEqual(reuse['tlsCompleted'], 2)
                    self.assertEqual(fresh['tlsCompleted'], 4)
                    for result in results:
                        self.assertEqual(result['statuses'], {'401': 4})
                        self.assertEqual(result['tlsResumed'], 0)
                        self.assertFalse(result['errors'])
                    self.assertEqual(seen, ['TLSv1.2']*6)
                finally:
                    server.close()
                    await server.wait_closed()

            asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
