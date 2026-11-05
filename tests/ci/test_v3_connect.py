"""Regressions for Connect isolation, resource limits, preserved offsets and plugin integrity."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
c=module('connect','scripts/check-v3-connect.py')
prepare=module('prepare','scripts/prepare-v3-connect-image.py')


class Connect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs=c.kafka.platform.load_documents(c.render([ROOT/'tests/ci/v3/connect.values.fixture.yaml']))
        cls.smoke=c.kafka.platform.load_documents(c.render([ROOT/'tests/ci/v3/connect-smoke.values.fixture.yaml']))

    def test_no_default_or_unpublished_foundation_runtime(self):
        for files in [[],[ROOT/'deploy/v3/environments/foundation/connect.values.yaml']]:c.validate(c.kafka.platform.load_documents(c.render(files)))
        self.assertEqual(len(self.docs),8);self.assertEqual(len(self.smoke),9)

    def test_pinned_crd_schema_and_runtime(self):
        for docs,probe in [(self.docs,False),(self.smoke,True)]:
            c.validate(docs,True,probe);c.kafka.platform.schema_check(yaml.safe_dump_all(docs))
        c.validate_image_source()

    def test_resource_node_heap_shutdown_and_secret_mutations_rejected(self):
        for what in ('memory','cpu','replicas','heap','node','mount','registry','grace'):
            docs=deepcopy(self.docs);s=next(d for d in docs if d['kind']=='KafkaConnect')['spec']
            if what=='memory':s['resources']['limits']['memory']='1Gi'
            if what=='cpu':s['resources']['limits']['cpu']='2'
            if what=='replicas':s['replicas']=2
            if what=='heap':s['jvmOptions']['-Xmx']='512m'
            if what=='node':s['template']['pod']['affinity']={}
            if what=='mount':s['template']['pod']['volumes']=[{'name':'other','secret':{'secretName':'production'}}]
            if what=='registry':s['template']['pod']['imagePullSecrets']=[]
            if what=='grace':s['template']['pod']['terminationGracePeriodSeconds']=10
            with self.subTest(what=what),self.assertRaises(ValueError):c.validate(docs,True)

    def test_bootstrap_tls_producer_and_db_injection_rejected(self):
        for what in ('bootstrap','plaintext','ca','password','acks','provider','override','db'):
            docs=deepcopy(self.docs);s=next(d for d in docs if d['kind']=='KafkaConnect')['spec']
            if what=='bootstrap':s['bootstrapServers']='kafka.production.yeodam.internal:9092'
            if what=='plaintext':s.pop('tls')
            if what=='ca':s['tls']['trustedCertificates'][0]['secretName']='wrong'
            if what=='password':s['authentication']['password']='literal'
            if what=='acks':s['config']['producer.acks']='1'
            if what=='provider':s['config']['config.providers']='kubernetes'
            if what=='override':s['config']['connector.client.config.override.policy']='All'
            if what=='db':docs.append({'kind':'KafkaConnector','metadata':{'name':'business'}})
            with self.subTest(what=what),self.assertRaises((ValueError,KeyError)):c.validate(docs,True)

    def test_worker_state_cannot_expire_or_change_topology(self):
        for what in ('compact','retention','partition','replica','keep','name'):
            docs=deepcopy(self.docs);t=next(d for d in docs if d['kind']=='KafkaTopic')
            if what=='compact':t['spec']['config']['cleanup.policy']='delete'
            if what=='retention':t['spec']['config']['retention.ms']=3600000
            if what=='partition':t['spec']['partitions']=3
            if what=='replica':t['spec']['replicas']=1
            if what=='keep':t['metadata'].pop('annotations')
            if what=='name':t['spec']['topicName']='prod.yeodam.trip-create.request'
            with self.subTest(what=what),self.assertRaises(ValueError):c.validate(docs,True)

    def test_network_peers_cannot_become_public_or_database_access(self):
        for what in ('all','port','peer','namespace','selected'):
            docs=deepcopy(self.docs);p=next(d for d in docs if d['metadata']['name']=='allow-v3-connect-to-brokers')
            if what=='all':p['spec']['ingress'][0]['from']=[{'podSelector':{}}]
            if what=='port':p['spec']['ingress'][0]['ports'][0]['port']=3306
            if what=='peer':p['spec']['ingress'][0]['from']=[{'ipBlock':{'cidr':'0.0.0.0/0'}}]
            if what=='namespace':p['metadata']['namespace']='production'
            if what=='selected':p['spec']['podSelector']={}
            with self.subTest(what=what),self.assertRaises(ValueError):c.validate(docs,True)

    def test_check_job_no_secrets_api_token_or_mutating_http(self):
        for what in ('token','secret','method','image','node'):
            docs=deepcopy(self.smoke);p=next(d for d in docs if d['kind']=='Job')['spec']['template']['spec'];tool=p['containers'][0]
            if what=='token':p['automountServiceAccountToken']=True
            if what=='secret':tool['envFrom']=[{'secretRef':{'name':'other'}}]
            if what=='method':tool['args'][0]+='\ncurl -X POST /connectors\n'
            if what=='image':tool['image']='unreviewed:latest'
            if what=='node':p['nodeSelector']={'yeodam.io/role':'worker'}
            with self.subTest(what=what),self.assertRaises(ValueError):c.validate(docs,True,True)

    def test_image_must_be_immutable_and_enabled_only_with_digest(self):
        for image in ('','quay.io/debezium/connect:latest','ghcr.io/100-hours-a-week/yeodam-v3-connect:sha-deadbeef'):
            with tempfile.TemporaryDirectory() as folder:
                p=Path(folder)/'bad.yaml';p.write_text(yaml.safe_dump({'enabled':True,'image':image}))
                with self.assertRaises(subprocess.CalledProcessError):c.render([p])

    def test_verified_plugin_checksum_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact={'url':'https://repo.maven.apache.org/maven2/test/test.jar','filename':'test.jar','bytes':4,'sha256':'0'*64}
            with patch.object(prepare,'LOCK',{'artifacts':[artifact]}),patch.object(prepare.urllib.request,'urlopen',return_value=io.BytesIO(b'test')):
                with self.assertRaises(ValueError):prepare.prepare(Path(folder))

    def test_plugin_archive_cannot_escape_or_symlink(self):
        for name,symlink in [('../escape.jar',False),('/absolute.jar',False),('link.jar',True)]:
            with tempfile.TemporaryDirectory() as folder:
                stream=io.BytesIO()
                with tarfile.open(fileobj=stream,mode='w:gz') as archive:
                    info=tarfile.TarInfo(name)
                    if symlink:info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
                    else:info.size=4
                    archive.addfile(info,None if symlink else io.BytesIO(b'test'))
                data=stream.getvalue();artifact={'url':'https://repo.maven.apache.org/maven2/test/test.tar.gz','filename':'test.tar.gz','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                with patch.object(prepare,'LOCK',{'artifacts':[artifact]}),patch.object(prepare.urllib.request,'urlopen',return_value=io.BytesIO(data)):
                    with self.assertRaises(ValueError):prepare.prepare(Path(folder))

    def test_stale_plugin_cannot_silently_enter_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'plugins').mkdir();(path/'plugins/old.jar').write_bytes(b'old')
            with patch.object(prepare,'LOCK',{'artifacts':[]}):
                with self.assertRaises(ValueError):prepare.prepare(path)

    def test_publication_is_main_only_tested_and_no_deploy(self):
        w=yaml.load((ROOT/'.github/workflows/v3-connect-image.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(w['on']['push']['branches'],['main'])
        steps=w['jobs']['build']['steps'];build=next(i for i,s in enumerate(steps) if 'bash scripts/validate-v3-outbox.sh' in s.get('run',''));publish=next(i for i,s in enumerate(steps) if 'docker push' in s.get('run',''))
        self.assertLess(build,publish)
        self.assertIn('export V3_OUTBOX_IMAGE="$V3_CONNECT_IMAGE"',steps[build]['run'])
        text=(ROOT/'.github/workflows/v3-connect-image.yml').read_text()
        for forbidden in ('kubectl','terraform apply','aws '):self.assertNotIn(forbidden,text)
        self.assertIn('"private"',text)


if __name__=='__main__':unittest.main()
