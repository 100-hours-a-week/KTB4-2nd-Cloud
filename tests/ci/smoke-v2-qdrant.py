#!/usr/bin/env python3
"""Actual pinned Docker snapshot/restore with synthetic data only; no AWS credentials."""
import argparse,importlib.util,json,os,shutil,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import qdrant_migration as q
spec=importlib.util.spec_from_file_location('restore',ROOT/'scripts/restore-v2-qdrant.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

def smoke(output):
    output.mkdir(parents=True,exist_ok=True)
    base=Path(tempfile.mkdtemp(prefix='qdrant-599-'));source=r.Runtime(base/'source','synthetic-source')
    original_cleanup=r.Runtime.cleanup
    def diagnostic_cleanup(runtime):
        if runtime.container_attempted:
            logs=r.docker('logs',runtime.name,check=False)
            if logs.returncode==0:(output/(runtime.role+'.log')).write_text(logs.stdout+logs.stderr)
        return original_cleanup(runtime)
    r.Runtime.cleanup=diagnostic_cleanup
    try:
        client=source.start()
        client.request('PUT',q.BASE,{'vectors':{'size':2048,'distance':'Cosine'}})
        points=[{'id':1,'vector':[0.3,0.4]+[0.0]*2046,'payload':{'fixture':'합성사진','photoId':41,'nested':{'empty':None}}},
            {'id':'123e4567-e89b-12d3-a456-426614174000','vector':[1.0]+[0.0]*2047,'payload':{'fixture':'vector-only','photoId':42}}]
        client.request('PUT',q.BASE+'/points?wait=true',{'points':points})
        client.request('PUT',q.BASE+'/index?wait=true',{'field_name':'photoId','field_schema':'integer'})
        client.request('POST','/collections/aliases',{'actions':[{'create_alias':{'collection_name':q.COLLECTION,'alias_name':'qdrant-599-synthetic-alias'}}]})
        restricted=r.DockerClient(source,source=True)
        proof={'account':q.ACCOUNT,'region':q.REGION,'instanceId':q.GPU,'container':q.CONTAINER,
            'image':q.IMAGE,'host':q.HOST,'port':6335,'readOnlyInspection':True,
            'collectionBytes':1024,'snapshotFreeBytes':shutil.disk_usage(base).free}
        package=base/'package';manifest=q.backup(restricted,package,proof,'synthetic')
        result=r.restore(package,base/'target')
        assert result['pointCount']==2 and result['vectorsMatch'] and result['aliasesMatch']
        assert q.fingerprint(restricted)==manifest['fingerprint'],'Synthetic source changed'
        bad=base/'corrupt';shutil.copytree(package,bad)
        with (bad/'collection.snapshot').open('ab') as f:f.write(b'tamper')
        try:r.restore(bad,base/'must-not-exist')
        except ValueError:pass
        else:raise AssertionError('Corrupt snapshot accepted')
        assert not (base/'must-not-exist').exists()
        result.update({'syntheticOnly':True,'sourceUnchanged':True,'corruptionRejectedBeforeRuntime':True,
            'snapshotBytes':manifest['snapshotBytes'],'fixtureMemoryMi':512,'fixtureCpu':0.5})
        source.cleanup()
        source.container_attempted=False;source.network_attempted=False
        result['runtimeCleaned']=not (base/'source').exists() and not (base/'target').exists()
        (output/'result.json').write_text(json.dumps(result,indent=2))
        print('PASS: pinned Qdrant collection snapshot/restore, full IDs/vectors/payload/config/aliases, TLS/API key, corruption refusal and cleanup')
    except Exception:
        # No real data ever runs here. Retain only synthetic container diagnostics on failure.
        for role in ['source','target']:
            names=r.docker('ps','-a','--filter','label=yeodam.issue=599','--format','{{.Names}}',check=False).stdout.split()
            for name in names:
                logs=r.docker('logs',name,check=False)
                (output/(name+'.log')).write_text(logs.stdout+logs.stderr)
        raise
    finally:
        source.cleanup()
        r.Runtime.cleanup=original_cleanup
        shutil.rmtree(base)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path)
    smoke(p.parse_args().output)
