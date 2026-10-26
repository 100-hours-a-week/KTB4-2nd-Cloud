import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('observer', Path(__file__).resolve().parents[1]/'scripts/nginx-connection-observer.py')
observer = importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)


class ConnectionObserverTest(unittest.TestCase):
    def test_stub_counters_are_not_inferred_from_vus(self):
        parsed=observer.parse_stub('Active connections: 12\nserver accepts handled requests\n 80 79 120\nReading: 1 Writing: 2 Waiting: 9\n')
        self.assertEqual(parsed,dict(active=12,accepts=80,handled=79,requests=120,reading=1,writing=2,waiting=9))
        with self.assertRaises(ValueError):observer.parse_stub('missing counters')

    def test_initial_short_cpu_interval_is_not_reported_as_one_second_peak(self):
        peaks=observer.peak_values([dict(cpuPercent=100,active=9),dict(cpuPercent=7,active=3)])
        self.assertEqual(peaks['cpuPercent'],7)
        self.assertEqual(peaks['active'],9)
        self.assertEqual(observer.peak_values([])['cpuPercent'],0)

    def test_response_is_bounded_and_connection_closed_on_failure(self):
        class Response:
            status=200
            def read(self,n):return b'x'*n
            def getheader(self,name):return 'public-request-id'
        class Connection:
            def __init__(self,*args,**kwargs):self.closed=False
            def request(self,*args,**kwargs):pass
            def getresponse(self):return Response()
            def close(self):self.closed=True
        connection=Connection()
        with patch.object(observer.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(ValueError):observer.request('/api/trips','private-cookie')
        self.assertTrue(connection.closed)


if __name__=='__main__':unittest.main()
