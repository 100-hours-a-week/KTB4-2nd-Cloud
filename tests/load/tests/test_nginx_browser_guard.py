import importlib.util
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('guard447',Path(__file__).parents[1]/'scripts/nginx-browser-guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class BrowserGuardTest(unittest.TestCase):
    def test_no_load_on_missing_stale_wrong_capture_or_incomplete_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);now=datetime.now(timezone.utc);run='owned-capture'
            self.assertFalse(guard.healthy(p,run,now))
            row={'version':1,'captureId':run,'origin':'https://v1-staging.yeodam-2gether.com',
                 'updatedAt':now.isoformat(),'ready':True,'failed':False,'result':{'ok':True},
                 'completedViews':['login','home','list','detail']}
            def write(): (p/'checkpoint.json').write_text(json.dumps(row))
            write();self.assertTrue(guard.healthy(p,run,now))
            self.assertFalse(guard.healthy(p,'another-capture',now))
            self.assertFalse(guard.healthy(p,run,now+timedelta(seconds=61)))
            row['ready']=False;write();self.assertFalse(guard.healthy(p,run,now))
            row['ready']=True;write();(p/'GUARD').touch();self.assertFalse(guard.healthy(p,run,now))


if __name__=='__main__': unittest.main()
