"""Failure contracts for private collection backups; never accesses AWS or Docker."""
import copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import qdrant_migration as q

def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
r=module('restore599','restore-v2-qdrant.py');b=module('backup599','backup-v2-qdrant.py')

def proof():
    return dict(account=q.ACCOUNT,region=q.REGION,instanceId=q.GPU,container=q.CONTAINER,image=q.IMAGE,
        host=q.HOST,port=6335,readOnlyInspection=True,snapshotFreeBytes=1024**3,collectionBytes=1024)

class Fixture(q.Client):
    def __init__(self):
        super().__init__(18599,source=True);self.points=[dict(id=1,vector=[1.0],payload={'fixture':'only'})]
        self.offset=None;self.count=1;self.version=q.VERSION;self.cluster='disabled';self.calls=[]
        self.config={'params':{'vectors':{'size':2048,'distance':'Cosine'}}}
    def request(self,method,path,body=None):
        self.allowed(method,path);self.calls.append((method,path))
        if path=='/':return {'version':self.version}
        if path=='/cluster':return {'status':self.cluster}
        if path==q.BASE:return dict(config=self.config,payload_schema={})
        if path.endswith('/aliases'):return {'aliases':[]}
        if path.endswith('/count'):return {'count':self.count}
        if path.endswith('/scroll'):return {'points':copy.deepcopy(self.points),'next_page_offset':self.offset}
        if path.endswith('/snapshots?wait=true'):return dict(name='fixture.snapshot',size=3,checksum=q.hashlib.sha256(b'abc').hexdigest())
        raise AssertionError(path)
    def download(self,name,path,size,sha):path.write_bytes(b'abc');path.chmod(0o600)

class Contracts(unittest.TestCase):
    def test_source_allowlist(self):
        client=q.Client(18599,source=True)
        for method,path in [('PUT',q.BASE),('DELETE',q.BASE),('POST','/collections/aliases'),('GET','/collections/staging'),
            ('GET',q.BASE+'/snapshots/../bad.snapshot'),('GET',q.BASE+'/snapshots/a.snapshot?other=1')]:
            with self.subTest(path=path),self.assertRaises(ValueError):client.allowed(method,path)
    def test_production_binding(self):
        for field,value in [('port',6333),('instanceId','other'),('image','latest'),('host','127.0.0.1'),('readOnlyInspection',False)]:
            p=proof();p[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):q.proof_valid(p,'synthetic')
    def test_disk_and_private_ci(self):
        p=proof();p['snapshotFreeBytes']=1
        with self.assertRaises(ValueError):q.proof_valid(p,'synthetic')
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),self.assertRaises(ValueError):q.proof_valid(proof(),'private-user-data')
    def test_version_cluster_vector_contract(self):
        for field,value in [('version','1.18.0'),('cluster','enabled'),('config',{'params':{'vectors':{'size':2,'distance':'Cosine'}}})]:
            f=Fixture();setattr(f,field,value)
            with self.subTest(field=field),self.assertRaises(ValueError):q.fingerprint(f)
    def test_count_duplicate_and_loop(self):
        for kind in ['count','duplicate','loop']:
            f=Fixture()
            if kind=='count':f.count=2
            if kind=='duplicate':f.points*=2
            if kind=='loop':f.points=[];f.offset=1
            with self.subTest(kind=kind),self.assertRaises(ValueError):q.fingerprint(f)
    def test_fingerprint_covers_payload_vector_ids(self):
        f=Fixture();original=q.fingerprint(f)
        for field,value,digest in [('payload',{'fixture':'changed'},'payloadsSha256'),('vector',[0.5],'vectorsSha256'),('id',2,'idsSha256')]:
            changed=Fixture();changed.points[0][field]=value
            self.assertNotEqual(original[digest],q.fingerprint(changed)[digest])
    def test_nan_rejected(self):
        f=Fixture();f.points[0]['vector']=[float('nan')]
        with self.assertRaises(ValueError):q.fingerprint(f)
    def test_package_integrity_and_no_runtime_on_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);package=base/'package';q.backup(Fixture(),package,proof(),'synthetic')
            self.assertEqual(q.verify_package(package)['snapshotBytes'],3)
            (package/'collection.snapshot').write_bytes(b'bad')
            with patch.object(r,'Runtime') as runtime,self.assertRaises(ValueError):r.restore(package,base/'target')
            runtime.assert_not_called()
    def test_symlink_and_extra_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);package=base/'package';q.backup(Fixture(),package,proof(),'synthetic')
            (package/'extra').write_text('x')
            with self.assertRaises(ValueError):q.verify_package(package)
            (package/'extra').unlink();(package/'collection.snapshot').rename(base/'snapshot')
            (package/'collection.snapshot').symlink_to(base/'snapshot')
            with self.assertRaises(ValueError):q.verify_package(package)
    def test_source_change_aborts_without_manifest(self):
        f=Fixture();original=f.download
        def download(*args):original(*args);f.points[0]['payload']={'changed':True}
        f.download=download
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'package'
            with self.assertRaises(ValueError):q.backup(f,path,proof(),'synthetic')
            self.assertFalse((path/'manifest.json').exists())
    def test_existing_package_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'keep').write_text('keep')
            with self.assertRaises(ValueError):q.backup(Fixture(),path,proof(),'synthetic')
            self.assertEqual((path/'keep').read_text(),'keep')
    def test_owned_session_parser(self):
        self.assertIsNone(b.session_id(b'no session'))
        self.assertEqual(b.session_id(b'Starting session with SessionId: user-a1-42\n'),'user-a1-42')
    def test_only_fixed_account_gpu_and_create_only_s3(self):
        source=(ROOT/'scripts/backup-v2-qdrant.py').read_text()
        self.assertIn("'--if-none-match','*'",source)
        for forbidden in ['start-instances','stop-instances','delete-object',"'restart'",'PutRolePolicy']:
            self.assertNotIn(forbidden,source)
    def test_source_proof_no_service_mutation(self):
        source=(ROOT/'scripts/migration/qdrant-source-proof.py').read_text()
        self.assertIn("'HostPort':'6335'",source)
        self.assertIn('QDRANT__SERVICE__API_KEY',source)
        for verb in ["'restart'","'stop'","'rm'","'kill'"]:self.assertNotIn(verb,source)
    def test_wrong_aws_account_stops_before_source_access(self):
        with patch.object(b.Aws,'__call__',return_value={'Account':'other'}) as api:
            with self.assertRaises(ValueError):b.Aws('test').preflight()
            self.assertEqual(api.call_count,1)
    def test_upload_requires_new_version_and_integrity(self):
        calls=[]
        def api(*args):
            calls.append(args)
            if args[1]=='put-object':return {'VersionId':'synthetic-version'}
            return dict(ContentLength=3,ServerSideEncryption='AES256',Metadata={'sha256':q.hashlib.sha256(b'abc').hexdigest()})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot';path.write_bytes(b'abc')
            with patch.object(b.Aws,'__call__',side_effect=api):
                self.assertEqual(b.Aws('test').upload(path,'migration/v2-v3/qdrant/test/snapshot'),'synthetic-version')
            self.assertIn('--if-none-match',calls[0]);self.assertIn('*',calls[0])
            self.assertIn('--version-id',calls[1]);self.assertIn('synthetic-version',calls[1])
            with patch.object(b.Aws,'__call__',return_value={}):
                with self.assertRaises(ValueError):b.Aws('test').upload(path,'unused')
    def test_private_package_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'package';q.backup(Fixture(),path,proof(),'synthetic')
            self.assertEqual(path.stat().st_mode&0o777,0o700)
            self.assertEqual((path/'manifest.json').stat().st_mode&0o777,0o600)

if __name__=='__main__':unittest.main()
