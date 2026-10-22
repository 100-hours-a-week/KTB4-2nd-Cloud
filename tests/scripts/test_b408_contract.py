import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
def load(name,script):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/script)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class ComparisonContract(unittest.TestCase):
    def test_limited_rate_uses_completed_control_only(self):
        m=load('compare408','run-b408-comparison.py')
        with tempfile.TemporaryDirectory() as folder,patch.object(m,'EVIDENCE',Path(folder)):
            m.initialize()
            case=json.loads((Path(folder)/'control/plan.json').read_text())['rates'][0]
            state=Path(folder)/'control'/(case['runId']+'.state.json')
            state.write_text(json.dumps({'phase':'preparing'}))
            with self.assertRaises(RuntimeError):m.configure_limited()
            state.write_text(json.dumps({'phase':'completed'}))
            (Path(folder)/'control/snapshot-stream.json').write_text(json.dumps({'configuredBytesPerSecond':0,'uncompressedBytes':10000000,'streamSeconds':10}))
            m.configure_limited()
            limited=json.loads((Path(folder)/'limited/plan.json').read_text())['rates'][0]
            self.assertEqual(limited['snapshotBytesPerSecond'],500000)
            self.assertEqual(limited['latestFenceSeconds'],600)
            with self.assertRaises(RuntimeError):m.configure_limited()

    def test_missing_or_failed_e2e_blocks_input(self):
        m=load('compare408','run-b408-comparison.py')
        with tempfile.TemporaryDirectory() as folder,patch.object(m,'EVIDENCE',Path(folder)):
            p=Path(folder)/'control';p.mkdir()
            (p/'e2e-preflight.json').write_text(json.dumps({'healthy':False,'registryPullAuthorized':True}))
            with self.assertRaises(RuntimeError):m.ensure_e2e_prepared('control')

    def test_bad_snapshot_rate_submits_no_command(self):
        m=load('runner408','b-integration376-run.py')
        with patch.object(m,'raw') as raw:
            with self.assertRaises(ValueError):m.start_snapshot({'snapshotBytesPerSecond':-1})
            raw.assert_not_called()


if __name__=='__main__':unittest.main()
