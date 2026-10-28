import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT=Path(__file__).parents[1]/'scripts/run-nginx-external-trials.py'


class ConnectionPlanTest(unittest.TestCase):
    def test_bounded_dry_run_preserves_legacy_default_and_requires_explicit_60(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);context=root/'context.json'
            data={'captureId':'ff92045f-a57a-456e-96ce-24ec2959c0e5','normalId':'d47170e6-f502-4c28-b151-bd3fad3db2a5',
                  'app':'i-05d320e3a87200d5a','generator':'i-0c53b65eb35dd2b42','ops':'/opt/yeodam/ops/nginx-edge438'}
            context.write_text(json.dumps(data))
            args=[sys.executable,str(SCRIPT),'--context',str(context),'--external',str(root/'external'),
                  '--output',str(root/'trials')]
            run=lambda extra:subprocess.run(args+extra,capture_output=True,text=True,timeout=5)
            result=run([]);self.assertEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout)['seconds'],30)
            result=run(['--seconds','60','--browser',str(root/'browser')])
            self.assertEqual(result.returncode,0);self.assertTrue(json.loads(result.stdout)['browserGuardEnabled'])
            self.assertFalse((root/'trials').exists())
            self.assertNotEqual(run(['--seconds','61']).returncode,0)
            data['app']='another-instance';context.write_text(json.dumps(data))
            self.assertNotEqual(run([]).returncode,0)


if __name__=='__main__':unittest.main()
