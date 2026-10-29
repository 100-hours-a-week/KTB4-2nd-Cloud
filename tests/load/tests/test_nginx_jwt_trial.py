from pathlib import Path
import subprocess
import importlib.util
import sys
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/nginx-jwt-read-trial.py'
class ApprovalGuardTest(unittest.TestCase):
    def test_missing_execution_or_wrong_approved_run_stops_before_host_or_session_access(self):
        run='11111111-1111-4111-a111-111111111111'
        common=[sys.executable,str(SCRIPT),'--case','unauth40','--role','generator',
                '--start-at','2099-01-01T00:00:00Z','--trial',run,'--session-run-id',run]
        for flags in [['--approved-run',run],['--execute','--approved-run','22222222-2222-4222-a222-222222222222']]:
            result=subprocess.run(common+flags,capture_output=True,text=True,timeout=3)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Explicit approved trial required',result.stderr)
            self.assertEqual(result.stdout,'')

    def test_nearest_rank_p95_is_not_the_maximum_for_twenty_observations(self):
        spec=importlib.util.spec_from_file_location('jwt_trial',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.percentile95(list(range(1,21))),19)
        self.assertEqual(module.percentile95([3]),3)
        self.assertIsNone(module.percentile95([]))

if __name__=='__main__': unittest.main()
