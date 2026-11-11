"""Bounded reference preparation, create-only supply, opt-in and existing APM contracts."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT/'deploy/v3/ai-apps/files/runtime'
sys.path.insert(0,str(RUNTIME))
import reference_data as refs

def module(name, path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

ai=module('tour_ai','scripts/check-v3-ai.py')
supply=module('tour_supply','scripts/prepare-v3-tourapi.py')
guard=module('tour_guard','scripts/check-v3-ai-plan.py')

class ReferenceTests(unittest.TestCase):
    def test_legacy_image_does_not_import_workflow(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(refs,'MARKER',Path(directory)/'ready'),patch.dict(os.environ,{'CLOUD_TOURAPI_ENABLED':'0'}):
            refs.prepare_tourapi(SimpleNamespace())
            self.assertTrue(refs.tourapi_ready())

    def test_ready_only_after_success_and_stale_marker_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tour.json';path.write_text('{"records":[1]}')
            marker=Path(directory)/'ready'
            env={'CLOUD_TOURAPI_ENABLED':'1','TOURAPI_RAW_PATH':str(path),'TOURAPI_RAW_SHA256':'a'*64}
            resources=ModuleType('app.pipeline.workflow.resources');resources.prepare_tour_raw=Mock(return_value=path)
            with patch.dict(os.environ,env),patch.object(refs,'MARKER',marker),patch.object(refs,'SIZE',path.stat().st_size),patch.dict(sys.modules,{'app.pipeline.workflow.resources':resources}):
                self.assertFalse(refs.tourapi_ready())
                refs.prepare_tourapi(SimpleNamespace(PROCESS_WORKFLOW_ENABLED=True))
                self.assertTrue(refs.tourapi_ready())
                resources.prepare_tour_raw.side_effect=ValueError('checksum mismatch')
                with self.assertRaises(ValueError):refs.prepare_tourapi(SimpleNamespace(PROCESS_WORKFLOW_ENABLED=True))
                self.assertFalse(marker.exists())
                self.assertFalse(refs.tourapi_ready())

    def test_unsupported_image_fails_when_opted_in(self):
        with tempfile.TemporaryDirectory() as d,patch.object(refs,'MARKER',Path(d)/'ready'),patch.dict(os.environ,{'CLOUD_TOURAPI_ENABLED':'1'}):
            with self.assertRaises(RuntimeError):refs.prepare_tourapi(SimpleNamespace())

    def test_startup_failure_never_starts_runner_or_marks_index_ready(self):
        events=[]
        cfgmod=ModuleType('app.jobs.config');cfgmod.JobsSettings=lambda:SimpleNamespace(PLACE_INDEX_SHA256='test')
        indexmod=ModuleType('app.infra.place_index');indexmod.prepare_place_index=lambda _:SimpleNamespace(stat=lambda:SimpleNamespace(st_size=1098801152))
        def fail(_):events.append('prepare');raise ValueError('invalid TourAPI')
        # No app.jobs.runner/control module supplied: reaching either would fail the test.
        with patch.dict(sys.modules,{'app.jobs.config':cfgmod,'app.infra.place_index':indexmod}),patch.object(refs,'prepare_tourapi',side_effect=fail),patch.object(Path,'unlink'),patch.object(Path,'write_text') as write:
            with self.assertRaisesRegex(ValueError,'invalid TourAPI'):runpy.run_path(str(RUNTIME/'start-runner.py'))
            write.assert_not_called()
        self.assertEqual(events,['prepare'])

class SupplyTests(unittest.TestCase):
    def test_size_and_hash_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'tour.json';p.write_bytes(b'bad')
            with self.assertRaises(ValueError):supply.verify(p)

    def test_create_only_and_wrong_account_block(self):
        payload=b'{"records":[1]}';sha=hashlib.sha256(payload).hexdigest()
        sts=Mock();sts.get_caller_identity.return_value={'Account':supply.ACCOUNT}
        s3=Mock();s3.put_object.return_value={};s3.head_object.return_value={'ContentLength':len(payload),'ChecksumSHA256':__import__('base64').b64encode(bytes.fromhex(sha)).decode()}
        session=Mock();session.client.side_effect=lambda service,**_:sts if service=='sts' else s3
        with patch.object(supply,'SIZE',len(payload)),patch.object(supply,'SHA',sha):
            result=supply.upload(session,payload)
            self.assertTrue(result['uploaded'])
            self.assertEqual(s3.put_object.call_args.kwargs['IfNoneMatch'],'*')
            self.assertEqual(s3.put_object.call_args.kwargs['Key'],supply.KEY)
            sts.get_caller_identity.return_value={'Account':'wrong'}
            s3.reset_mock()
            with self.assertRaises(ValueError):supply.upload(session,payload)
            s3.put_object.assert_not_called()

    def test_collision_is_not_overwritten_or_retried(self):
        payload=b'{"records":[1]}';sts=Mock();sts.get_caller_identity.return_value={'Account':supply.ACCOUNT}
        s3=Mock();s3.put_object.side_effect=RuntimeError('PreconditionFailed')
        session=Mock();session.client.side_effect=lambda service,**_:sts if service=='sts' else s3
        with patch.object(supply,'SIZE',len(payload)),patch.object(supply,'SHA',hashlib.sha256(payload).hexdigest()):
            with self.assertRaises(RuntimeError):supply.upload(session,payload)
            self.assertEqual(s3.put_object.call_count,1)
            s3.delete_object.assert_not_called()

class ChartTests(unittest.TestCase):
    def test_opt_in_preserves_apm_and_minimum_resources(self):
        values=ai.fixture();values['apmWorkloads']=['api']
        before=ai.platform.load_documents(ai.render(values))
        values['tourapiEnabled']=True
        after=ai.platform.load_documents(ai.render(values));ai.validate(after,values)
        ai.platform.schema_check(ai.render(values))
        for old in (d for d in before if d['kind']=='Deployment'):
            new=next(d for d in after if d['kind']=='Deployment' and d['metadata']['name']==old['metadata']['name'])
            a=copy.deepcopy(old);b=copy.deepcopy(new)
            for item in [a,b]:item['spec']['template']['metadata']['annotations'].pop('checksum/runtime')
            self.assertEqual(a,b)
        cm={d['metadata']['name']:d['data'] for d in after if d['kind']=='ConfigMap'}
        self.assertNotIn('TOURAPI_RAW_PATH',cm['ai-api-runtime'])
        self.assertEqual(cm['ai-runner-runtime']['CLOUD_TOURAPI_ENABLED'],'1')
        self.assertEqual(cm['ai-api-runtime']['GPU_GENERATION_INFLIGHT'],'8')
        self.assertEqual(cm['ai-runner-runtime']['JOBS_PROCESS_TIMEOUT_SEC'],'3900')

    def test_only_exact_policy_addition_allowed(self):
        objects=['arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-uploads/*/derived/*','arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-additions/*/derived/*','arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite']
        before={'name':'read-v3-derived-photos','policy':json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':objects}]})}
        doc=json.loads(before['policy']);doc['Statement'][0]['Resource'].append(guard.TOURAPI)
        after=dict(before,policy=json.dumps(doc))
        p={'complete':True,'resource_changes':[{'mode':'managed','type':'aws_iam_role_policy','address':'aws_iam_role_policy.read["ai-runner"]','change':{'actions':['update'],'before':before,'after':after}}]}
        self.assertEqual(guard.validate(p),{'creates':0,'updates':1,'deletes':0})
        doc['Statement'][0]['Resource'].append('*');after['policy']=json.dumps(doc)
        with self.assertRaises(ValueError):guard.validate(p)

if __name__=='__main__':unittest.main()
