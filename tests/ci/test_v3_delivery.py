"""Full release, publisher provenance, adoption and read-only observation boundaries."""
import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests/ci/v3')]
import v3_release_bundle as bundle
import v3_release_candidate as candidate_check
from full_release_fixture import candidate
from prepare_v3_candidate_helpers import local_charts


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observer = load('observe-v3-release')
adoption = load('check-v3-adoption')
updater = load('update-v3-release-candidate')
plan = load('check-v3-delivery-plan')


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = candidate()
        cls.files = bundle.build(cls.data, local_charts(), {'candidatePr': 1, 'cloudCommit': 'e'*40, 'cloudGateRunId': 2})
        cls.docs = bundle.apps.platform.load_documents(cls.files['manifests/release.yaml'].decode())

    def publisher_api(self, skipped=False):
        def api(path):
            for name, c in self.data['components'].items():
                repo, _, workflow = bundle.COMPONENTS[name]
                publisher = candidate_check.CLOUD if name == 'websocket' else repo
                runpath = f"repos/{publisher}/actions/runs/{c['buildRunId']}"
                if path == runpath:
                    return {'id': c['buildRunId'], 'head_sha': c['buildSourceSha'], 'head_branch': 'main', 'path': '.github/workflows/'+workflow,
                            'event': 'push', 'status': 'completed', 'conclusion': 'success', 'repository': {'full_name': publisher}, 'head_repository': {'full_name': publisher}}
                if path == runpath+'/jobs?filter=latest&per_page=100':
                    return {'jobs': [{'steps': [{'name': 'Record immutable deployment input' if name == 'websocket' else 'Notify Cloud V2 release manifest',
                                               'conclusion': 'skipped' if skipped else 'success'}]}]}
                if path == f"repos/{repo}/compare/{c['sourceSha']}...main":
                    return {'status': 'ahead'}
                if name == 'websocket' and path.endswith('/source.json?ref='+c['buildSourceSha']):
                    value = {'sourceRepository': repo, 'sourceSha': c['sourceSha'], 'image': c['image'].split('@')[0]+':sha-'+c['sourceSha']}
                    return {'content': base64.b64encode(json.dumps(value).encode()).decode()}
            raise AssertionError(path)
        return api

    def test_successful_publisher_and_tag_digest_all_four_components(self):
        def inspect(args, **kwargs):
            tag = args[4]
            return next(c['image'].split('@')[1]+'\n' for c in self.data['components'].values() if tag.endswith(c['sourceSha']))
        with patch.object(candidate_check.subprocess, 'check_output', side_effect=inspect):
            candidate_check.verify_components(self.data, self.publisher_api())
        with patch.object(candidate_check.subprocess, 'check_output', return_value='sha256:'+'f'*64), self.assertRaises(ValueError):
            candidate_check.verify_components(self.data, self.publisher_api())

    def test_merged_candidate_requires_exact_file_current_main_and_required_jobs(self):
        cloud = candidate_check.CLOUD
        head, merge = 'a'*40, 'b'*40
        content = {'content': base64.b64encode(json.dumps(self.data).encode()).decode()}
        responses = {
            f'repos/{cloud}/pulls/9': {'merged': True, 'changed_files': 1, 'merge_commit_sha': merge, 'base': {'ref': 'main', 'repo': {'full_name': cloud}}, 'head': {'sha': head, 'ref': 'release/v3-candidate', 'repo': {'full_name': cloud}}},
            f'repos/{cloud}/pulls/9/files?per_page=100': [{'filename': candidate_check.PATH, 'status': 'modified'}],
            f'repos/{cloud}/actions/workflows/cloud-pr-gate.yml/runs?head_sha={head}&event=pull_request&per_page=100': {'workflow_runs': [{'id': 10, 'head_sha': head, 'head_branch': 'release/v3-candidate', 'event': 'pull_request', 'path': '.github/workflows/cloud-pr-gate.yml', 'head_repository': {'full_name': cloud}, 'status': 'completed', 'conclusion': 'success'}]},
            f'repos/{cloud}/actions/runs/10/jobs?filter=latest&per_page=100': {'jobs': [{'name': n, 'conclusion': 'success'} for n in ['cloud-ci-gate', 'V3 candidate evidence / V3 candidate evidence']]}}
        for ref in [head, merge, 'main']:
            responses[f'repos/{cloud}/contents/{candidate_check.PATH}?ref={ref}'] = content
        with patch.object(candidate_check, 'verify_components'):
            self.assertTrue(candidate_check.verify_merged('9', lambda p: responses[p])['candidateVerified'])
            responses[f'repos/{cloud}/actions/runs/10/jobs?filter=latest&per_page=100']['jobs'][1]['conclusion'] = 'skipped'
            with self.assertRaises(ValueError):
                candidate_check.verify_merged('9', lambda p: responses[p])

    def test_skipped_publication_is_not_a_valid_candidate(self):
        with self.assertRaises(ValueError):
            candidate_check.verify_components(self.data, self.publisher_api(skipped=True))

    def test_nine_roles_common_images_and_order_without_pod_annotations(self):
        deployments = [d for d in self.docs if d['kind'] == 'Deployment']
        self.assertEqual(len(deployments), 9)
        for d in deployments:
            name = d['metadata']['name']
            self.assertEqual(d['metadata']['annotations']['argocd.argoproj.io/sync-wave'], str(bundle.WAVES[name]))
            self.assertNotIn('argocd.argoproj.io/sync-wave', d['spec']['template']['metadata'].get('annotations', {}))
            self.assertEqual(d['spec']['replicas'], 1)
        be = [d for d in deployments if d['metadata']['name'] in bundle.PROFILE_ROLES['backend']]
        self.assertEqual({d['spec']['template']['spec']['containers'][0]['image'] for d in be}, {self.data['components']['backend']['image']})

    def test_domain_private_key_missing_role_and_tampered_manifest_fail(self):
        for mutate in [lambda d: d['values']['websocket']['defaults']['configMapData'].update(WEBSOCKET_ALLOWED_ORIGINS='https://other.example'),
                       lambda d: d['values']['ai']['trust'].update(kafkaCa='-----BEGIN PRIVATE KEY-----'),
                       lambda d: d['values']['backend']['workloads'].pop('photo-converter')]:
            data = deepcopy(self.data)
            mutate(data)
            with self.assertRaises((ValueError, KeyError)):
                bundle.build(data, local_charts(), {'candidatePr': 1, 'cloudCommit': 'e'*40, 'cloudGateRunId': 2})
        files = dict(self.files)
        files['manifests/release.yaml'] += b'\n# modified\n'
        with self.assertRaises(ValueError):
            bundle.validate(files)
        with self.assertRaises(ValueError):
            bundle.parse_json('{"state":"a","state":"b"}')

    def test_ownership_adoption_requires_identical_templates_and_no_secrets(self):
        live = {'kind': 'List', 'items': deepcopy(self.docs)}
        self.assertTrue(adoption.verify(self.files, live)['ownershipOnly'])
        d = next(d for d in live['items'] if d['kind'] == 'Deployment')
        d['spec']['template']['spec']['containers'][0]['image'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'Pod template differs'):
            adoption.verify(self.files, live)
        with self.assertRaisesRegex(ValueError, 'Never collect Secret'):
            adoption.verify(self.files, {'kind': 'List', 'items': [{'kind': 'Secret'}]})

    def test_observation_requires_exact_revision_all_nine_and_healthy(self):
        app = {'metadata': {'name': 'yeodam-v3-apps', 'namespace': 'argocd'},
               'spec': {'project': 'yeodam-v3-apps', 'source': {'repoURL': 'https://github.com/100-hours-a-week/KTB4-2nd-Cloud.git', 'targetRevision': 'deploy/v3-approved', 'path': 'manifests'}},
               'status': {'sync': {'status': 'Synced', 'revision': 'a'*40}, 'health': {'status': 'Healthy'},
                          'resources': [{'kind': 'Deployment', 'name': n, 'namespace': ns, 'status': 'Synced', 'health': {'status': 'Healthy'}} for n, ns in observer.APP_NAMES.items()]}}
        self.assertTrue(observer.status(app, 'a'*40)['deployed'])
        self.assertFalse(observer.status(app, 'b'*40)['deployed'])
        app['status']['resources'].pop()
        self.assertFalse(observer.status(app, 'a'*40)['deployed'])
        app['spec']['source']['targetRevision'] = 'main'
        with self.assertRaises(ValueError):
            observer.status(app, 'a'*40)

    def test_stale_notifications_and_diverged_source_do_not_promote(self):
        c = deepcopy(self.data['components']['backend'])
        with patch.object(updater, 'verify_components', side_effect=AssertionError('No registry read')):
            self.assertFalse(updater.update(self.data, 'backend', c)[1])
            c['sourceSha'] = c['buildSourceSha'] = 'f'*40
            with patch.object(updater, 'api', return_value={'status': 'behind'}):
                self.assertFalse(updater.update(self.data, 'backend', c)[1])
            with patch.object(updater, 'api', return_value={'status': 'diverged'}), self.assertRaises(ValueError):
                updater.update(self.data, 'backend', c)

    def test_notification_waits_for_publisher_completion_without_deploying(self):
        with patch.object(updater, 'api', side_effect=[{'status': 'in_progress'}, {'status': 'completed', 'conclusion': 'success'}]), patch.object(updater.time, 'sleep') as sleep:
            updater.wait_for_publication('owner/repo', 1)
            sleep.assert_called_once_with(5)
        with patch.object(updater, 'api', return_value={'status': 'completed', 'conclusion': 'failure'}), self.assertRaises(ValueError):
            updater.wait_for_publication('owner/repo', 1)

    def test_plan_rejects_non_observer_or_production_mutation(self):
        self.assertEqual(plan.validate({})['creates'], 0)
        for action in ['create', 'update', 'delete']:
            with self.assertRaises(ValueError):
                plan.validate({'resource_changes': [{'address': 'aws_instance.production', 'change': {'actions': [action]}}]})

    def test_no_execution_permissions_or_unapproved_tunnel_in_observer(self):
        text = (ROOT/'terraform/v3/delivery/main.tf').read_text()
        for forbidden in ['ssm:SendCommand', 'secretsmanager:GetSecretValue', 'AmazonEKSClusterAdminPolicy', 'aws_instance', 'aws_launch_template']:
            self.assertNotIn(forbidden, text)
        source = (ROOT/'scripts/observe-v3-release.py').read_text()
        self.assertNotIn('CERT_NONE', source)
        self.assertIn('server_hostname=self.host', source)
        self.assertIn("'ssm','terminate-session'", source)
