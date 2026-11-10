"""Previously approved AI Chart remains recoverable, arbitrary legacy payloads do not."""
import copy,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests/ci/v3')]
import v3_release_bundle as bundle
from full_release_fixture import candidate
from prepare_v3_candidate_helpers import local_charts

class Compatibility(unittest.TestCase):
    def legacy(self):
        charts=local_charts()
        for name in list(charts):
            if name.startswith('charts/ai-apps/'):del charts[name]
        old=json.loads((ROOT/'tests/ci/v3/legacy-ai-chart.fixture.json').read_text())
        charts.update({'charts/ai-apps/'+p:value.encode() for p,value in old.items()})
        return charts
    def test_exact_prior_approved_chart_can_be_rebuilt_and_validated(self):
        files=bundle.build(candidate(),self.legacy(),{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})
        self.assertEqual(bundle.validate(files)['schemaVersion'],2)
    def test_modified_legacy_runtime_is_not_accepted(self):
        for path in ['files/runtime/start-runner.py','files/contract.json','templates/resources.yaml']:
            charts=self.legacy();key='charts/ai-apps/'+path;charts[key]+=b'\n# not the approved source\n'
            with self.subTest(path=path),self.assertRaises(ValueError):
                bundle.build(candidate(),charts,{'candidatePr':1,'cloudCommit':'a'*40,'cloudGateRunId':1})

if __name__=='__main__':unittest.main()
