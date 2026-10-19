import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
def load(name):
    spec=importlib.util.spec_from_file_location('runner_'+name,ROOT/'monitoring/ai-runner'/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class RunnerOps(unittest.TestCase):
    def test_structured_logs_keep_trace_fields(self):
        m=load('logs');event={'log':json.dumps({'event':'photo_decision','trip_id':3,'job_id':'job-1','duration_ms':42,'api_key':'private','message':'Bearer abc.def'}),'time':'2026-10-19T12:00:00Z'}
        body=json.loads(m.normalize(event,'production'))
        self.assertEqual(body['job_id'],'job-1');self.assertEqual(body['duration_ms'],42);self.assertEqual(body['env'],'production')
        self.assertNotIn('private',json.dumps(body));self.assertNotIn('abc.def',json.dumps(body))
    def test_docker_log_path_cannot_read_other_files(self):
        m=load('logs');cid='a'*64
        self.assertTrue(m.valid_log('/var/lib/docker/containers/'+cid+'/'+cid+'-json.log'))
        self.assertFalse(m.valid_log('/etc/yeodam/ai/runner.env'))
        self.assertFalse(m.valid_log('/var/lib/docker/containers/'+cid+'/'+'b'*64+'-json.log'))
    def test_level_edit_preserves_other_settings_and_removes_duplicates(self):
        m=load('level');result=m.updated_env('REDIS_URL=private\nLOG_LEVEL=DEBUG\nAPI_KEY=private\nLOG_LEVEL=INFO\n','INFO')
        self.assertEqual(result.count('LOG_LEVEL='),1);self.assertIn('REDIS_URL=private\n',result);self.assertIn('API_KEY=private\n',result)
    def test_busy_and_queued_refuse_without_file_or_container_mutation(self):
        for result in (0,-1):
            m=load('level')
            with tempfile.TemporaryDirectory() as directory:
                m.SETTINGS={'staging':{'lock':directory+'/lock','redis':'ai-redis.staging.yeodam.internal','env':directory+'/runner.env'}}
                client=unittest.mock.Mock();client.command.return_value=result
                row={'Config':{'Image':'image'}};values={'JOBS_ENV':'staging','REDIS_URL':'private'}
                with patch.object(m,'inspect',return_value=(row,values,'INFO')),patch.object(m,'Redis',return_value=client),patch.object(m,'docker') as docker,patch.object(m,'atomic') as write:
                    with self.assertRaisesRegex(RuntimeError,'busy'):m.change('staging','DEBUG')
                    docker.assert_not_called();write.assert_not_called();client.close.assert_called_once()
    def test_recreation_failure_restores_env_and_releases_only_own_slot(self):
        m=load('level')
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'runner.env';config.write_text('SECRET=private\nLOG_LEVEL=INFO\n')
            m.SETTINGS={'staging':{'lock':directory+'/lock','redis':'ai-redis.staging.yeodam.internal','env':str(config),'root':directory,'container':'runner'}}
            client=unittest.mock.Mock();marker={}
            def command(*args):
                if args[0]=='EVAL' and args[1]==m.RESERVE:marker['value']=args[-1];return 1
                if args[0]=='GET':return marker['value']
                return 1
            client.command.side_effect=command
            row={'Config':{'Image':'image'}};values={'JOBS_ENV':'staging','REDIS_URL':'private'}
            with patch.object(m,'inspect',return_value=(row,values,'INFO')),patch.object(m,'Redis',return_value=client),patch.object(m,'Path',side_effect=lambda s:Path(directory)/'journal.json' if str(s).startswith('/var/lib/') else Path(s)),patch.object(m,'docker',side_effect=[RuntimeError('recreate failed'),'']):
                with self.assertRaisesRegex(RuntimeError,'recreate failed'):m.change('staging','DEBUG')
            self.assertEqual(config.read_text(),'SECRET=private\nLOG_LEVEL=INFO\n')
            self.assertTrue(any(c.args[:2]==('EVAL',m.RELEASE) for c in client.command.call_args_list))
    def test_interrupted_env_edit_reconciles_without_restarting_same_level(self):
        m=load('level')
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'runner.env';config.write_text('SECRET=private\nLOG_LEVEL=DEBUG\n')
            m.SETTINGS={'staging':{'lock':directory+'/lock','redis':'host','env':str(config)}}
            row={'Config':{'Image':'image'},'_configured_level':'DEBUG'};values={'JOBS_ENV':'staging','REDIS_URL':'private'}
            client=unittest.mock.Mock()
            with patch.object(m,'inspect',return_value=(row,values,'INFO')),patch.object(m,'Redis',return_value=client),patch.object(m,'docker') as docker:
                m.change('staging','INFO');docker.assert_not_called();client.command.assert_not_called()
            self.assertEqual(config.read_text(),'SECRET=private\nLOG_LEVEL=INFO\n')
    def test_rotation_and_container_replacement_keep_fixed_output(self):
        import datetime,os
        m=load('logs')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);m.STATE=root/'state';m.OUTPUT=root/'output';m.STATE.mkdir();m.OUTPUT.mkdir()
            source=root/'docker.log'
            def event(message):return json.dumps({'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'log':json.dumps({'message':message})})+'\n'
            source.write_text(event('first'))
            def inspect(*args,**kwargs):return unittest.mock.Mock(stdout=json.dumps({'LogPath':str(source),'HostConfig':{'LogConfig':{'Type':'json-file'}}}))
            with patch.object(m,'valid_log',return_value=True),patch.object(m.subprocess,'run',side_effect=inspect):
                self.assertEqual(m.collect('staging','runner'),1)
                with source.open('a') as f:f.write(event('before rotate'))
                source.rename(Path(str(source)+'.1'));source.write_text(event('after rotate'))
                self.assertEqual(m.collect('staging','runner'),2)
                source=root/'replacement.log';source.write_text(event('new container'))
                self.assertEqual(m.collect('staging','runner'),1)
                self.assertEqual(m.collect('staging','runner'),0)
            rows=[json.loads(x)['message'] for x in (m.OUTPUT/'staging.jsonl').read_text().splitlines()]
            self.assertEqual(rows,['first','before rotate','after rotate','new container'])
    def test_source_logs_are_separate_from_agent_destinations(self):
        config=json.loads((ROOT/'monitoring/ai-runner/cloudwatch.json').read_text())
        rows=config['logs']['logs_collected']['files']['collect_list']
        self.assertEqual({r['log_group_name'] for r in rows},{'/yeodam/v2/staging/ai-runner','/yeodam/v2/production/ai-runner'})
        self.assertEqual(len({r['file_path'] for r in rows}),2)
if __name__=='__main__':unittest.main()
