from pathlib import Path
import importlib.util
import json
import io
from contextlib import redirect_stdout
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'monitoring/v1-staging'))
from nginx_user_read_profile import BACKEND_DIGEST
s=importlib.util.spec_from_file_location('render468',ROOT/'scripts/render-v1-staging-nginx.py');renderer=importlib.util.module_from_spec(s);s.loader.exec_module(renderer)
s=importlib.util.spec_from_file_location('prepare468',ROOT/'monitoring/v1-staging/jwt-gate/prepare.py');prepare=importlib.util.module_from_spec(s);s.loader.exec_module(prepare)

class PreparationTest(unittest.TestCase):
    def test_stage_keeps_active_files_and_commands_untouched_and_key_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);site=root/'site';main=root/'main';stage=root/'stage'
            site.write_text(renderer.render((ROOT/'nginx/yeodam.conf').read_text(),'a'*40,user_read_enabled=True,backend_digest=BACKEND_DIGEST));main.write_text('original main')
            before=site.read_bytes();config={'secret':'synthetic-key-for-private-stage-468','issuer':'https://issuer.test','audience':'yeodam-api','clockSkewSeconds':60}
            args=SimpleNamespace(expected_site_sha=prepare.sha(site),expected_main_sha=prepare.sha(main))
            with patch.multiple(prepare,SITE=site,MAIN=main,STAGE=stage),patch.object(prepare,'backend',return_value=config),patch.object(prepare,'run') as run:
                receipt=prepare.stage(args)
                self.assertEqual(site.read_bytes(),before);self.assertEqual(main.read_text(),'original main');run.assert_not_called()
                self.assertNotIn(config['secret'],json.dumps(receipt))
                self.assertEqual(stat.S_IMODE((stage/'config.json').stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(stage.stat().st_mode),0o700)
                self.assertIn('yeodam_jwt_users',(stage/'site-candidate.conf').read_text())
                with self.assertRaises(FileExistsError):prepare.stage(args)

    def test_drift_or_missing_confirmation_prevents_activation(self):
        with self.assertRaises(ValueError):prepare.activate(SimpleNamespace(confirm_instance='i-wrong'))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);site=root/'site';main=root/'main';stage=root/'stage';stage.mkdir()
            site.write_text('changed');main.write_text('original main')
            (stage/'receipt.json').write_text(json.dumps({'beforeSha':'0'*64,'mainSha':prepare.sha(main)}))
            with patch.multiple(prepare,SITE=site,MAIN=main,STAGE=stage),patch.object(prepare,'backend') as backend,patch.object(prepare,'run') as run:
                with self.assertRaises(ValueError):prepare.activate(SimpleNamespace(confirm_instance=prepare.APP))
                backend.assert_not_called();run.assert_not_called()

    def test_redeploy_checks_runtime_key_without_printing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            live=Path(tmp)
            config={'secret':'synthetic-key-for-redeploy-468','issuer':'https://issuer.test','audience':'yeodam-api','clockSkewSeconds':60}
            (live/'config.json').write_text(json.dumps(config))
            with patch.object(prepare,'LIVE',live),patch.object(prepare,'backend',return_value=config),patch.object(prepare,'assert_host'),patch.object(sys,'argv',['prepare.py','verify-runtime']):
                out=io.StringIO()
                with redirect_stdout(out):prepare.main()
                self.assertTrue(json.loads(out.getvalue())['runtimeMatchesPreloadedConfiguration'])
                self.assertNotIn(config['secret'],out.getvalue())
            with patch.object(prepare,'LIVE',live),patch.object(prepare,'backend',return_value={**config,'secret':'changed-key'}),patch.object(prepare,'assert_host'),patch.object(sys,'argv',['prepare.py','verify-runtime']):
                with self.assertRaisesRegex(ValueError,'JWT key/issuer changed'):prepare.main()

if __name__=='__main__':unittest.main()
