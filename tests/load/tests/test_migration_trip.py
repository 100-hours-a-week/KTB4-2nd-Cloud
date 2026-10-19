import importlib.util
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


script = Path(__file__).parents[1] / 'scripts' / 'reconcile-migration-trip.py'
spec = importlib.util.spec_from_file_location('migration_trip_reconcile', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

attest_script = Path(__file__).parents[1] / 'scripts' / 'attest-migration-trip.py'
attest_spec = importlib.util.spec_from_file_location('migration_trip_attest', attest_script)
attest_module = importlib.util.module_from_spec(attest_spec)
attest_spec.loader.exec_module(attest_module)

RUN = '6b942bca-39b9-40c7-8af4-8c098e2c3262'
TRIP = 't2e3617d34'
ROW = ('1000005\t707\tt2e3617d34\t2025-01-01\t2025-01-03\tPROCESSING\t'
       '1000005\t50110\tECA09CECA3BC\t33.49953424\t126.53117109\n')


class MigrationTripReconcileTest(unittest.TestCase):
    def run_case(self, status=201, outcome='confirmed', source=ROW, target=ROW, trip_id=1000005,
                 mode='baseline', include_final=True):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            events = folder / 'events.jsonl'
            source_path = folder / 'source.tsv'
            target_path = folder / 'target.tsv'
            request = {'tripName': TRIP, 'startDate': '2025-01-01',
                       'endDate': '2025-01-03', 'regionCodes': ['50110']}
            lines = [
                {'source': 'migration-trip-create', 'event': 'started', 'runId': RUN,
                 'userId': 707, 'tripName': TRIP, 'request': request},
            ]
            if include_final:
                lines.append({'source': 'migration-trip-create', 'event': 'final', 'runId': RUN,
                              'userId': 707, 'tripName': TRIP, 'status': status,
                              'tripId': trip_id, 'outcome': outcome})
            events.write_text(''.join(json.dumps(line) + '\n' for line in lines))
            source_path.write_text(source)
            target_path.write_text(target)
            return module.reconcile(events, source_path, target_path, RUN, mode)

    def test_confirmed_request_must_match_both_tables_on_both_databases(self):
        report = self.run_case()
        self.assertTrue(report['passed'])
        self.assertEqual(report['requests'][0]['classification'], 'confirmed_and_replicated')

    def test_timeout_with_committed_rows_is_not_http_success(self):
        report = self.run_case(status=0, outcome='unconfirmed', trip_id=None)
        self.assertFalse(report['passed'])
        self.assertEqual(report['requests'][0]['classification'], 'committed_without_confirmed_http')

    def test_missing_target_and_duplicate_region_fail(self):
        self.assertFalse(self.run_case(target='')['passed'])
        duplicate = ROW + ROW.replace('\t1000005\t50110\t', '\t1000006\t50110\t')
        report = self.run_case(source=duplicate, target=duplicate)
        self.assertFalse(report['passed'])
        self.assertEqual(report['requests'][0]['source'], 'region_mismatch')

    def test_cutover_accepts_confirmed_target_only_trip_but_baseline_does_not(self):
        result = self.run_case(source='', mode='cutover')
        self.assertTrue(result['passed'])
        self.assertEqual(result['targetOnlyRequests'], 1)
        self.assertEqual(result['requests'][0]['classification'], 'confirmed_on_target')
        self.assertFalse(self.run_case(source='', mode='baseline')['passed'])

    def test_interrupted_request_is_kept_as_failure(self):
        result = self.run_case(include_final=False)
        self.assertFalse(result['passed'])
        self.assertEqual(result['final'], 0)

    def test_live_attestation_separates_auth_refresh_from_trip_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / 'events.jsonl'
            records = [
                {'source': 'migration-trip-create', 'event': 'run_started', 'runId': RUN,
                 'mode': 'live', 'rate': 10, 'duration': 2700},
                {'source': 'migration-trip-create', 'event': 'auth_refreshed', 'runId': RUN,
                 'vu': 1, 'status': 200},
                {'source': 'migration-trip-create', 'event': 'started', 'runId': RUN,
                 'userId': 707, 'tripName': TRIP,
                 'request': {'tripName': TRIP, 'startDate': '2025-01-01',
                             'endDate': '2025-01-03', 'regionCodes': ['50110']}},
                {'source': 'migration-trip-create', 'event': 'final', 'runId': RUN,
                 'userId': 707, 'tripName': TRIP, 'status': 201,
                 'tripId': 1000005, 'outcome': 'confirmed'},
            ]
            events.write_text(''.join(json.dumps(row) + '\n' for row in records))
            result = attest_module.attest_events(events, RUN, 10, True, 2700, 'live')
            self.assertEqual((result['trips'], result['authRefreshes'],
                              result['authRefreshFailures']), (1, 1, 0))

    def check_full_plan(self, rate):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            events, source, target, summary = (folder / name for name in
                                               ('events.jsonl', 'source.tsv.gz', 'target.tsv.gz', 'summary.json'))
            lines = [{'source': 'migration-trip-create', 'event': 'run_started', 'runId': RUN,
                      'mode': 'load', 'rate': rate, 'duration': 120}]
            rows = []
            planned = rate * 120
            for index in range(planned):
                name = 't' + format(index, '09x')
                trip_id = 1000005 + index
                request = {'tripName': name, 'startDate': '2025-01-01',
                           'endDate': '2025-01-03', 'regionCodes': ['50110']}
                lines.extend((
                    {'source': 'migration-trip-create', 'event': 'started', 'runId': RUN,
                     'userId': 707, 'tripName': name, 'request': request},
                    {'source': 'migration-trip-create', 'event': 'final', 'runId': RUN,
                     'userId': 707, 'tripName': name, 'status': 201,
                     'tripId': trip_id, 'outcome': 'confirmed'},
                ))
                rows.append(f'{trip_id}\t707\t{name}\t2025-01-01\t2025-01-03\tPROCESSING\t'
                            f'{trip_id}\t50110\tECA09CECA3BC\t33.49953424\t126.53117109\n')
            events.write_text(''.join(json.dumps(line) + '\n' for line in lines))
            for path in (source, target):
                with gzip.open(path, 'wt') as stream:
                    stream.writelines(rows)
            metrics = {'trip_create_started': {'values': {'count': planned}},
                       'trip_create_final_failures': {'values': {'count': 0}},
                       'dropped_iterations': {'values': {'count': 0}}}
            summary.write_text(json.dumps({'metrics': metrics}))
            report = module.reconcile(events, source, target, RUN, summary=summary, expected_rate=rate)
            self.assertTrue(report['passed'])
            self.assertEqual(report['plannedRequests'], planned)
            if rate == 30:
                event_attestation = attest_module.attest_events(events, RUN)
                source_attestation = attest_module.attest_rows(source, RUN)
                target_attestation = attest_module.attest_rows(target, RUN)
                self.assertEqual(event_attestation['projectionSha256'], source_attestation['projectionSha256'])
                self.assertEqual(source_attestation['fullRowsSha256'], target_attestation['fullRowsSha256'])
                pre_switch = folder / 'pre-switch.tsv.gz'
                with gzip.open(pre_switch, 'wt') as stream:
                    stream.writelines(rows[:planned // 2])
                source_prefix = attest_module.attest_rows(pre_switch, RUN)
                target_prefix = attest_module.attest_rows(target, RUN, source_prefix['maxTripId'])
                self.assertEqual(source_prefix['trips'], target_prefix['trips'])
                self.assertEqual(source_prefix['fullRowsSha256'], target_prefix['fullRowsSha256'])
                failed_lines = list(lines)
                failed_lines[2] = {**failed_lines[2], 'status': 500, 'outcome': 'unconfirmed'}
                events.write_text(''.join(json.dumps(line) + '\n' for line in failed_lines))
                with self.assertRaises(ValueError):
                    attest_module.attest_events(events, RUN)
                events.write_text(''.join(json.dumps(line) + '\n' for line in lines))
            metrics['dropped_iterations']['values']['count'] = 1
            summary.write_text(json.dumps({'metrics': metrics}))
            self.assertFalse(module.reconcile(events, source, target, RUN, summary=summary,
                                              expected_rate=rate)['passed'])

    def test_full_10_rps_plan_requires_all_1200_events_and_no_drops(self):
        self.check_full_plan(10)

    def test_full_30_rps_plan_requires_all_3600_events_and_no_drops(self):
        self.check_full_plan(30)

    def test_incomplete_100_rps_keeps_ambiguous_requests_for_host_hash_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            events, source, target = (folder / name for name in ('events.jsonl', 'source.tsv.gz', 'target.tsv.gz'))
            lines = [{'source': 'migration-trip-create', 'event': 'run_started', 'runId': RUN,
                      'mode': 'load', 'rate': 100, 'duration': 120}]
            rows = []
            for index in range(2):
                name = f'tpartial{index}'
                request = {'tripName': name, 'startDate': '2025-01-01',
                           'endDate': '2025-01-03', 'regionCodes': ['50110']}
                lines.append({'source': 'migration-trip-create', 'event': 'started', 'runId': RUN,
                              'userId': 707, 'tripName': name, 'request': request})
                rows.append(f'{1000005 + index}\t707\t{name}\t2025-01-01\t2025-01-03\tPROCESSING\t'
                            f'{1000005 + index}\t50110\tECA09CECA3BC\t33.49953424\t126.53117109\n')
            lines.append({'source': 'migration-trip-create', 'event': 'final', 'runId': RUN,
                          'userId': 707, 'tripName': 'tpartial0', 'status': 0,
                          'tripId': None, 'outcome': 'unconfirmed'})
            events.write_text(''.join(json.dumps(line) + '\n' for line in lines))
            for path in (source, target):
                with gzip.open(path, 'wt') as stream:
                    stream.writelines(rows)
            with self.assertRaises(ValueError):
                attest_module.attest_events(events, RUN, expected_rate=100)
            event_result = attest_module.attest_events(events, RUN, expected_rate=100, allow_incomplete=True)
            source_result = attest_module.attest_rows(source, RUN)
            target_result = attest_module.attest_rows(target, RUN)
            self.assertEqual((event_result['trips'], event_result['final'], event_result['confirmed'],
                              event_result['unconfirmed'], event_result['missingFinal']), (2, 1, 0, 1, 1))
            self.assertIsNone(event_result['projectionSha256'])
            self.assertEqual(event_result['requestProjectionSha256'], source_result['requestProjectionSha256'])
            self.assertEqual(source_result['fullRowsSha256'], target_result['fullRowsSha256'])
            self.assertFalse(module.reconcile(events, source, target, RUN, expected_rate=100)['passed'])

    def test_trip_runner_bounds_terminal_output_and_preserves_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_k6 = folder / 'k6'
            fake_k6.write_text('#!/bin/sh\nif [ "$1" = version ]; then echo "k6 v2.3.0"; exit 0; fi\n'
                               'i=0; while [ "$i" -lt 100 ]; do echo "Request Failed $i"; i=$((i+1)); done\n'
                               'exit 17\n')
            fake_k6.chmod(0o755)
            load_root = Path(__file__).parents[1]
            env = dict(os.environ, PATH=f'{folder}:{os.environ["PATH"]}', K6_RESULTS_DIRECTORY=str(folder / 'results'))
            run = subprocess.run(['bash', 'scripts/run-k6.sh', 'scenarios/migration-trip-create.js'],
                                 cwd=load_root, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(run.returncode, 17)
            self.assertLess(run.stdout.count('Request Failed'), 30)
            result_dir = next((folder / 'results').iterdir())
            self.assertEqual((result_dir / 'run.log').read_text().count('Request Failed'), 100)
            self.assertIn('exit_code=17', (result_dir / 'execution.txt').read_text())


if __name__ == '__main__':
    unittest.main()
