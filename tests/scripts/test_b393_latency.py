import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/name)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class LatencyTests(unittest.TestCase):
    def test_completion_parser_keeps_old_records_and_splits_only_instrumented_records(self):
        app = load('b-integration376-app.py')
        run = '26b45888-9b29-4afb-b090-ff2acbfc3ab6'
        text = ('migration_trip_completed request_id=11111111-1111-1111-1111-111111111111 origin=source queued_at_ms=1000 committed_at_ms=1400 consumer_started_at_ms=1300 apply_elapsed_us=99000 partition=2\n'
                'migration_trip_completed request_id=22222222-2222-2222-2222-222222222222 origin=target queued_at_ms=1000 committed_at_ms=1500\n')
        payload = text.encode();frame = b'\x01\x00\x00\x00'+len(payload).to_bytes(4,'big')+payload
        connection = Mock();response = Mock(status=200);response.read.return_value=frame;connection.getresponse.return_value=response
        with tempfile.TemporaryDirectory() as directory:
            actual_path = Path(directory)/run/'completions.json';actual_path.parent.mkdir()
            # Only redirect the known private run file; no log data is emitted.
            with patch.object(app,'Connection',return_value=connection),patch.object(app,'Path',return_value=Path(directory)):
                result=app.collect_completions('owned',run)
            self.assertEqual(result['count'],2)
            self.assertEqual(result['timingCount'],1)
            self.assertEqual(result['timingByOrigin']['source']['queueWaitMs']['p95'],300)
            self.assertEqual(result['timingByOrigin']['source']['applyElapsedUs']['p95'],99000)
            self.assertEqual(result['timingByOrigin']['target']['count'],0)
            self.assertEqual(result['timingByOrigin']['source']['partitionCounts']['2'],1)

    def test_resource_delta_is_per_core_and_ignores_reused_pid(self):
        sampler=load('b-integration393-resources.py')
        before=([100,0,100,800,0,0,0,0],{(10,1):('java',100),(11,1):('gzip',500)})
        after=([200,0,200,1600,0,0,0,0],{(10,1):('java',200),(11,2):('gzip',900)})
        result=sampler.delta(before,after,2,100)
        self.assertEqual(result['hostBusyPercent'],20)
        self.assertEqual(result['processCpuOneCorePercent'],{'java':50})

    def test_minute_buckets_follow_command_time_not_delayed_consumer_start(self):
        module=load('summarize-b393-latency.py')
        rows={'a':{'origin':'source','partition':0,'queuedAtMs':1000,'consumerStartedAtMs':60000,'committedAtMs':60001,'applyElapsedUs':1000},
              'b':{'origin':'target','partition':1,'queuedAtMs':61000,'consumerStartedAtMs':63000,'committedAtMs':63002,'applyElapsedUs':2000},
              'legacy':{'origin':'source','queuedAtMs':1000,'committedAtMs':2000}}
        result=module.summarize(rows,0)
        self.assertEqual(result['timedRecords'],2)
        self.assertEqual(result['totalRecords'],3)
        self.assertEqual(result['byCommandMinute']['0']['queueWaitMs']['p95'],59000)
        self.assertEqual(result['byCommandMinute']['1']['applyElapsedMs']['p95'],2)

if __name__=='__main__':unittest.main()
