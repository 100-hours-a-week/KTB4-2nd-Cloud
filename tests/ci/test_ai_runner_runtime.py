import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
m = load('runtime491', 'scripts/ai_runner_runtime.py')
seed = load('seed491', 'scripts/seed-v2-ai-runner-kakao.py')
OLD = {'VLM_BASE_URL': 'https://oldpod-8000.proxy.runpod.net/v1', 'VLM_API_KEY': 'secret-old', 'VLM_MODEL': 'gemma-4'}
NEW = dict(OLD, VLM_BASE_URL='https://newpod-8000.proxy.runpod.net/v1')


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / 'runner.env'
        self.env.write_text(m.render_env(dict(OLD, JOBS_ENV='staging', REDIS_URL='redis://unchanged')))
        self.backups = self.root / 'backups'
        self.real_path = Path
        self.row = {'Id': 'original', 'Image': 'immutable-image', 'RestartCount': 0, 'State': {'Running': True},
                    'Mounts': [{'Source': m.HOST_PATH, 'Destination': m.CONTAINER_PATH, 'RW': True}],
                    'Config': {'Env': self.env.read_text().splitlines()}}
        self.environments = patch.dict(m.ENVIRONMENTS, {'staging': (str(self.root), str(self.env), 'runner', 'project')})
        self.environments.start(); self.addCleanup(self.environments.stop)

    def path(self, value):
        return self.backups if value == '/run/yeodam-runner-settings' else self.real_path(value)

    def action(self, action, version=3, operation='a'*32, expected=NEW, source_version=None):
        source_version = source_version if source_version is not None else version if action in {'inspect-settings', 'restore-vlm'} else 3
        with patch.object(m, 'Path', side_effect=self.path), patch.object(m, 'container', side_effect=lambda env: copy.deepcopy(self.row)), \
             patch.object(m, 'vlm_values', return_value=(expected, source_version)), patch.object(m, 'runner_ready') as ready, \
             patch.object(m, 'same_image'), patch.object(m, 'run') as command:
            result = m.url_action('staging', action, version, operation)
            self.last_ready = ready.call_args
            return result, command.call_args_list

    def preflight(self):
        return self.action('inspect-settings', version=2, expected=OLD)

    def test_prepare_preserves_existing_runtime_keys_and_writes_only_after_validation(self):
        with patch.object(m, 'additional_values', return_value=dict(NEW, PLACE_INDEX_SHA256=m.INDEX_SHA)), patch.object(m, 'storage') as storage:
            m.prepare('staging', 'reviewed-image')
        values=m.parse_env(self.env.read_text())
        self.assertEqual(values['REDIS_URL'], 'redis://unchanged')
        self.assertEqual(values['VLM_BASE_URL'], NEW['VLM_BASE_URL'])
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        storage.assert_called_once_with('reviewed-image')
        before=self.env.read_bytes()
        with patch.object(m, 'additional_values', side_effect=RuntimeError('missing Parameter')), patch.object(m, 'storage') as storage:
            with self.assertRaises(RuntimeError): m.prepare('staging','image')
            storage.assert_not_called()
        self.assertEqual(before,self.env.read_bytes())

    def test_sync_and_restore_preserve_unrelated_settings_and_target_only_runner(self):
        self.preflight()
        result, commands=self.action('sync-vlm')
        self.assertEqual(result, {'settingsReady':True,'parameterVersion':3})
        self.assertEqual(m.parse_env(self.env.read_text())['REDIS_URL'],'redis://unchanged')
        self.assertEqual(commands[0].args, ('docker','compose','up','-d','--no-deps','--no-build','--pull','never','--force-recreate','process-runner'))
        backup=self.backups/'staging'/('a'*32+'.json')
        self.assertEqual(backup.stat().st_mode & 0o777,0o600)
        self.row['Config']['Env']=self.env.read_text().splitlines()
        self.action('restore-vlm',expected=OLD)
        self.assertEqual(m.parse_env(self.env.read_text())['VLM_BASE_URL'],OLD['VLM_BASE_URL'])
        self.assertFalse(backup.exists())

    def test_stale_version_wrong_environment_and_operation_replay_do_not_write(self):
        self.preflight()
        before=self.env.read_text()
        with self.assertRaises(RuntimeError):self.action('sync-vlm',version=2)
        self.assertEqual(before,self.env.read_text())
        with self.assertRaises(ValueError):self.action('sync-vlm',operation='../escape')
        self.action('sync-vlm')
        self.row['Config']['Env']=self.env.read_text().splitlines()
        with self.assertRaises(RuntimeError):self.action('sync-vlm')
        self.env.write_text('JOBS_ENV=production\n')
        with self.assertRaises(ValueError):self.action('inspect-settings')

    def test_restore_refuses_external_file_image_and_runtime_environment_changes(self):
        self.preflight()
        self.action('sync-vlm')
        new=self.env.read_text()
        self.row['Config']['Env']=new.splitlines()
        self.env.write_text(new+'EXTERNAL_CHANGE=yes\n')
        with self.assertRaises(RuntimeError):self.action('restore-vlm',expected=OLD)
        self.env.write_text(new)
        self.row['Image']='other-image'
        with self.assertRaises(RuntimeError):self.action('restore-vlm',expected=OLD)
        self.row['Image']='immutable-image'
        self.row['Config']['Env'].append('EXTERNAL_RUNTIME=yes')
        with self.assertRaises(RuntimeError):self.action('restore-vlm',expected=OLD)

    def test_url_update_does_not_require_index_kakao_or_mounts(self):
        self.env.write_text('JOBS_ENV=staging\nREDIS_URL=redis://unchanged\n')
        original=self.env.read_text()
        self.row['Config']['Env']=original.splitlines()
        self.row['Mounts']=[]
        with patch.object(m,'storage') as storage, patch.object(m,'additional_values') as additional:
            result,_=self.preflight()
            self.assertTrue(result['settingsReady'])
            self.assertTrue(result['initializingVlm'])
            self.action('sync-vlm')
            storage.assert_not_called();additional.assert_not_called()
        values=m.parse_env(self.env.read_text())
        self.assertEqual(values, dict(NEW,JOBS_ENV='staging',REDIS_URL='redis://unchanged'))
        self.row['Config']['Env']=self.env.read_text().splitlines()
        self.action('restore-vlm',version=4,expected=OLD)
        self.assertEqual(self.env.read_text(),original)
        self.assertEqual(self.last_ready.args[1],{})
        self.assertEqual(set(self.last_ready.kwargs['absent']),set(m.VLM_KEYS))
        self.assertFalse(self.last_ready.kwargs['probe_vlm'])

    def test_existing_vlm_may_be_stale_but_unsaved_runtime_drift_is_rejected(self):
        # Runner can be aligned with its file while lagging behind API settings.
        result,_=self.action('inspect-settings',version=2,expected=NEW)
        self.assertTrue(result['settingsReady'])
        self.row['Config']['Env'].append('VLM_API_KEY=external')
        with self.assertRaises(ValueError):self.action('inspect-settings',operation='b'*32)

    def test_sync_requires_a_matching_preflight_and_restore_before_sync_is_noop(self):
        with self.assertRaises(RuntimeError):self.action('sync-vlm')
        self.preflight()
        self.env.write_text(self.env.read_text()+'EXTERNAL=yes\n')
        with self.assertRaises(RuntimeError):self.action('sync-vlm')
        self.env.write_text(m.render_env(dict(OLD,JOBS_ENV='staging',REDIS_URL='redis://unchanged')))
        result,commands=self.action('restore-vlm',version=4,expected=OLD)
        self.assertTrue(result['runnerUnchanged']);self.assertFalse(commands)

    def test_restore_uses_original_api_url_not_the_runners_absent_or_stale_url(self):
        self.preflight()
        self.action('sync-vlm')
        self.row['Config']['Env']=self.env.read_text().splitlines()
        with self.assertRaises(RuntimeError):self.action('restore-vlm',version=4,expected=NEW)
        self.action('restore-vlm',version=4,expected=OLD)

    def test_same_image_guard_blocks_a_different_local_compose_image(self):
        config=json.dumps({'services':{'process-runner':{'image':'local-configured-image'}}})
        with patch.object(m,'run',side_effect=[config,'original-image']):
            m.same_image('staging',{'Image':'original-image'})
        with patch.object(m,'run',side_effect=[config,'different-image']),self.assertRaises(RuntimeError):
            m.same_image('staging',{'Image':'original-image'})

    def test_partial_vlm_configuration_is_restored_byte_for_byte(self):
        original='# Keep this comment\nJOBS_ENV=staging\nVLM_MODEL=gemma-4\nVLM_API_KEY=\nREDIS_URL=redis://unchanged\n'
        self.env.write_text(original)
        self.row['Config']['Env']=[line for line in original.splitlines() if line and not line.startswith('#')]
        self.row['Mounts']=[]
        self.preflight();self.action('sync-vlm')
        self.row['Config']['Env']=self.env.read_text().splitlines()
        self.action('restore-vlm',version=4,expected=OLD)
        self.assertEqual(self.env.read_text(),original)
        self.assertEqual(self.last_ready.args[1],{'VLM_MODEL':'gemma-4','VLM_API_KEY':''})
        self.assertEqual(self.last_ready.kwargs['absent'],['VLM_BASE_URL'])

    def test_restore_readiness_verifies_absence_without_calling_an_unconfigured_model(self):
        self.row['Config']['Env']=['JOBS_ENV=staging']
        with patch.object(m,'container',return_value=self.row),patch.object(m.time,'sleep'),patch.object(m,'run') as command:
            m.runner_ready('staging',{},absent=m.VLM_KEYS,probe_vlm=False,image_id='immutable-image')
            command.assert_not_called()
        self.row['Config']['Env'].append('VLM_BASE_URL=still-present')
        with patch.object(m,'container',return_value=self.row),patch.object(m.time,'sleep'),patch.object(m,'run') as command:
            with self.assertRaises(TimeoutError):
                m.runner_ready('staging',{},absent=m.VLM_KEYS,probe_vlm=False,image_id='immutable-image')
            command.assert_not_called()

    def test_stopped_runner_or_wrong_preflight_version_stops_without_snapshot(self):
        self.row['State']['Running']=False
        with self.assertRaises(RuntimeError):self.preflight()
        self.assertFalse(list(self.backups.rglob('*.json')))
        self.row['State']['Running']=True
        with self.assertRaises(RuntimeError):self.action('inspect-settings',version=2,source_version=3)
        self.assertFalse(list(self.backups.rglob('*.json')))

    def test_url_coordinator_and_host_script_initialize_and_restore_real_env_files(self):
        client=load('client493','scripts/update-v2-runpod.py')
        # Connect both production code paths. Only AWS/Docker/network boundaries
        # are mocked; the snapshot, env writes, version checks and rollback run.
        for original_vlm,fail in [({},False),({},True),({'VLM_MODEL':'gemma-4'},True),(OLD,False)]:
            with self.subTest(keys=list(original_vlm),fail=fail):
                original=m.render_env(dict(original_vlm,JOBS_ENV='staging',REDIS_URL='redis://unchanged'))
                self.env.write_text(original)
                self.row['Config']['Env']=original.splitlines();self.row['Mounts']=[]
                api={'Value':OLD['VLM_BASE_URL'],'Version':2}
                def aws(*args):
                    if args[:2]==('sts','get-caller-identity'):return {'Account':client.ai.ACCOUNT}
                    if args[:2]==('ssm','get-parameter'):return {'Parameter':copy.deepcopy(api)}
                    if args[:2]==('ssm','put-parameter'):
                        api.update(Value=args[args.index('--value')+1],Version=api['Version']+1)
                        return {'Version':api['Version']}
                    if args[:2]==('ecs','register-task-definition'):return {'taskDefinition':{'taskDefinitionArn':'new'}}
                    return {}
                def source(env):return dict(OLD,VLM_BASE_URL=api['Value']),api['Version']
                def recreate(env):self.row['Config']['Env']=self.env.read_text().splitlines()
                def ready(env,expected,**options):
                    if fail and options.get('probe_vlm',True):raise RuntimeError('model verification failed')
                    actual=m.parse_env('\n'.join(self.row['Config']['Env']))
                    self.assertTrue(all(actual.get(k)==v for k,v in expected.items()))
                    self.assertTrue(all(k not in actual for k in options.get('absent',[])))
                definition={'containerDefinitions':[{'environment':[{'name':'VLM_MODEL','value':'gemma-4'}]}]}
                with patch.object(client.ai,'aws',side_effect=aws), \
                     patch.object(client,'service',return_value={'taskDefinition':'old'}), \
                     patch.object(client.ai,'stable',return_value=True), \
                     patch.object(client,'target_group',return_value='target'), \
                     patch.object(client,'healthy_tasks',return_value=True), \
                     patch.object(client,'definition',return_value=definition), \
                     patch.object(client,'probe'),patch.object(client,'wait_api'), \
                     patch.object(client,'runner_settings',side_effect=m.url_action), \
                     patch.object(m,'Path',side_effect=self.path), \
                     patch.object(m,'container',side_effect=lambda env:copy.deepcopy(self.row)), \
                     patch.object(m,'vlm_values',side_effect=source),patch.object(m,'same_image'), \
                     patch.object(m,'recreate',side_effect=recreate),patch.object(m,'runner_ready',side_effect=ready), \
                     patch.object(m,'storage') as storage,patch.object(m,'additional_values') as additions:
                    report={'status':'preflight'}
                    if fail:
                        with self.assertRaises(RuntimeError):
                            client.promote('staging',NEW['VLM_BASE_URL'],report,lambda:None)
                        self.assertEqual(report['status'],'restored_previous')
                        self.assertEqual(self.env.read_text(),original)
                        self.assertEqual(api['Value'],OLD['VLM_BASE_URL'])
                    else:
                        client.promote('staging',NEW['VLM_BASE_URL'],report,lambda:None)
                        self.assertEqual(report['status'],'healthy')
                        self.assertEqual(api['Value'],NEW['VLM_BASE_URL'])
                        self.assertEqual(m.parse_env(self.env.read_text()),dict(NEW,JOBS_ENV='staging',REDIS_URL='redis://unchanged'))
                    storage.assert_not_called();additions.assert_not_called()
                self.assertFalse(list(self.backups.rglob('*.json')))

    def test_environment_sources_are_exact_and_values_never_returned_as_receipts(self):
        calls=[]
        def parameter(name):
            calls.append(name)
            return {'Value':NEW['VLM_BASE_URL'] if name.endswith('VLM_BASE_URL') else 'secret', 'Version':3}
        for env in m.ENVIRONMENTS:
            calls.clear()
            with patch.object(m,'parameter',side_effect=parameter):values=m.additional_values(env)
            self.assertEqual(set(calls),{f'/yeodam/v2/{env}/ai/VLM_BASE_URL',f'/yeodam/v2/{env}/ai/VLM_API_KEY',f'/yeodam/v2/{env}/app/KAKAO_CLIENT_ID'})
            self.assertEqual(values['PLACE_INDEX_SHA256'],m.INDEX_SHA)
            self.assertEqual(values['PLACE_INDEX_PATH'],m.CONTAINER_PATH+'/'+m.INDEX)

    def test_storage_resolves_nonroot_image_user_checks_space_and_does_not_chown_existing_other_user(self):
        with tempfile.TemporaryDirectory() as root, patch.object(m,'HOST_PATH',root), \
             patch.object(m,'run',return_value='[999,999]'), patch.object(m.shutil,'disk_usage') as usage, patch.object(m.os,'chown') as chown:
            usage.return_value.free=100
            with self.assertRaises(RuntimeError):m.storage('image')
            chown.assert_not_called()
            usage.return_value.free=3_000_000_000
            m.storage('image');chown.assert_called_once_with(Path(root),999,999)
            Path(root,'existing.sqlite').write_text('keep')
            # Test host owner differs from container UID; no recursive ownership change.
            if Path(root).stat().st_uid!=999:
                with self.assertRaises(RuntimeError):m.storage('image')
            with patch.object(m,'run',return_value='[0,0]'):
                with self.assertRaises(ValueError):m.storage('image')

    def test_malformed_environment_rejected_without_echoing_values(self):
        for text in ['KEY=secret\nKEY=other\n','INVALID secret']:
            with self.assertRaises(ValueError) as error:m.parse_env(text)
            self.assertNotIn('secret',str(error.exception))
        with self.assertRaises(ValueError):m.render_env({'KEY':'line\nbreak'})

    def test_container_guard_rejects_another_compose_project_or_entrypoint(self):
        row = {'Config': {'Labels': {'com.docker.compose.project': 'project'}, 'Cmd': ['python','-m','app.jobs.runner']}}
        with patch.object(m, 'run', return_value=json.dumps([row])):
            self.assertEqual(m.container('staging'), row)
        row['Config']['Labels']['com.docker.compose.project'] = 'unrelated-service'
        with patch.object(m, 'run', return_value=json.dumps([row])), self.assertRaises(ValueError):
            m.container('staging')


class SeedTest(unittest.TestCase):
    def exercise(self, current=None, apply=False):
        calls=[]
        def aws(*args,**kwargs):
            calls.append(args)
            if args[:2]==('sts','get-caller-identity'):return {'Account':seed.ACCOUNT}
            if args[:2]==('ssm','get-parameter'):return current
            if args[:2]==('ssm','put-parameter'):
                self.assertNotIn('--overwrite',args)
                path=Path(args[-1].removeprefix('file://'))
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(json.loads(path.read_text())['Type'],'SecureString')
                return {'Version':1}
            raise AssertionError(args)
        with patch.object(seed,'aws',side_effect=aws),patch.object(seed,'source_value',return_value='secret'):
            result=seed.seed('staging',apply)
        self.assertNotIn('secret',json.dumps(result))
        return result,calls

    def test_dry_run_never_writes_and_explicit_apply_only_creates_missing_parameter(self):
        result,calls=self.exercise();self.assertEqual(result['status'],'missing')
        self.assertFalse(any(x[:2]==('ssm','put-parameter') for x in calls))
        result,calls=self.exercise(apply=True);self.assertEqual(result['status'],'created')
        self.assertEqual(sum(x[:2]==('ssm','put-parameter') for x in calls),1)

    def test_existing_values_are_preserved_and_conflicts_rejected(self):
        result,calls=self.exercise({'Parameter':{'Value':'secret','Version':1}},True)
        self.assertEqual(result['status'],'preserved')
        self.assertFalse(any(x[:2]==('ssm','put-parameter') for x in calls))
        with self.assertRaises(RuntimeError):self.exercise({'Parameter':{'Value':'different','Version':1}},True)

if __name__=='__main__':unittest.main()
