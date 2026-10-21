import importlib.util,io,json,tempfile,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/name);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class QueueEvidenceTests(unittest.TestCase):
 def test_http500_and_missing_response_cannot_disappear_from_non202_evidence(self):
  m=load('b-integration376-generator.py');run='26b45888-9b29-4afb-b090-ff2acbfc3ab6'
  with tempfile.TemporaryDirectory() as folder:
   directory=Path(folder)/'state';directory.mkdir();root=Path(folder)/'results';result=root/'once';result.mkdir(parents=True)
   (directory/'k6.json').write_text(json.dumps({'runId':run,'rate':10,'startedAt':1}))
   records=[]
   for rid,status in [('one',202),('two',500),('three',None)]:
    records.append({'event':'started','runId':run,'requestId':rid,'userId':7,'tripName':rid,'request':{'startDate':'2026-01-01','endDate':'2026-01-02','regionCodes':['50110']}})
    if status is not None:records.append({'event':'first_response','runId':run,'requestId':rid,'status':status,'elapsedMs':1})
   (result/'trip-events.jsonl').write_text('\n'.join(json.dumps(v) for v in records))
   with patch.object(m,'paths',return_value=(directory,root)),redirect_stdout(io.StringIO()):m.read(run,True)
   state=json.loads((directory/'result.json').read_text())
   self.assertEqual(state['non202Requests'],2);self.assertEqual(state['firstStatuses'],{'202':1,'500':1})
if __name__=='__main__':unittest.main()
