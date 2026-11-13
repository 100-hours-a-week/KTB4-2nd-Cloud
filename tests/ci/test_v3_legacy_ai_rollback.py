"""Previously approved AI Chart remains recoverable, arbitrary legacy payloads do not."""
import copy,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests/ci/v3')]
import v3_release_bundle as bundle
from full_release_fixture import candidate
from prepare_v3_candidate_helpers import local_charts

class Compatibility(unittest.TestCase):
    def legacy(self,fixture='legacy-ai-chart.fixture.json'):
        charts=local_charts()
        for name in list(charts):
            if name.startswith('charts/ai-apps/'):del charts[name]
        old=json.loads((ROOT/'tests/ci/v3'/fixture).read_text())
        charts.update({'charts/ai-apps/'+p:value.encode() for p,value in old.items()})
        return charts
    def test_exact_prior_approved_chart_can_be_rebuilt_and_validated(self):
        files=bundle.build(candidate(),self.legacy(),{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})
        self.assertEqual(bundle.validate(files)['schemaVersion'],2)
    def test_previous_adapter_chart_remains_recoverable(self):
        charts=self.legacy('prior-adapter-ai-chart.fixture.json')
        files=bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})
        self.assertEqual(bundle.validate(files)['schemaVersion'],2)
        charts['charts/ai-apps/files/runtime/runpod_control.py']+=b'\n# unapproved modification\n'
        with self.assertRaises(ValueError):
            bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})

    def test_initial_apm_approved_chart_remains_recoverable(self):
        files=bundle.build(candidate(),self.legacy('prior-tourapi-ai-chart.fixture.json'),{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})
        self.assertEqual(bundle.validate(files)['schemaVersion'],2)

    def test_previous_reliability_chart_and_hook_are_hash_pinned(self):
        charts=self.legacy('prior-reliability-ai-chart.fixture.json')
        files=bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})
        self.assertEqual(bundle.validate(files)['schemaVersion'],2)
        charts['charts/ai-apps/files/apm/sitecustomize.py']+=b'\n# unapproved hook\n'
        with self.assertRaises(ValueError):
            bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})

    def test_modified_legacy_runtime_is_not_accepted(self):
        for path in ['files/runtime/start-runner.py','files/contract.json','templates/resources.yaml']:
            charts=self.legacy();key='charts/ai-apps/'+path;charts[key]+=b'\n ' if path.endswith('.json') else b'\n# not the approved source\n'
            with self.subTest(path=path),self.assertRaises(ValueError):
                bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})

if __name__=='__main__':unittest.main()
