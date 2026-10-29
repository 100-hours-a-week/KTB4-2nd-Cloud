from pathlib import Path
import subprocess
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

if __name__=='__main__': unittest.main()
