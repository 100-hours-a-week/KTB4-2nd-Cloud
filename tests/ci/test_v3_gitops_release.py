from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import v3_gitops_release as m

POLICY={'schemaVersion':1,'target':'yeodam-v3-foundation','deploymentEnabled':False,'branch':m.BRANCH,'environment':m.ENVIRONMENT,'deliveryUnits':['frontend'],'adoptionReviewed':False}
CTX={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':m.CLOUD,'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main','GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}


class GitOpsReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        manifest=m.legacy.checks.manifest.load(ROOT/m.legacy.checks.MANIFEST)
        cls.candidate={'status':'disabled','candidateVerified':True,'pr':100,'cloudCommit':cls.source,'ciRunId':200,'manifest':manifest,'images':m.legacy.checks.image_refs(manifest)}
        cls.bundle=m.build(cls.candidate)
        cls.release_id=json.loads(cls.bundle['release.json'])['releaseId']

    def test_default_policy_and_deterministic_public_snapshot(self):
        self.assertEqual(m.read_policy(),POLICY)
        self.assertEqual(m.build(self.candidate),self.bundle)
        meta=m.validate_bundle(self.bundle)
        self.assertEqual(meta['evidence']['cloudCommit'],self.source)
        self.assertNotIn('metadata',self.bundle)
        self.assertTrue(all(not n.startswith('.github') for n in self.bundle))

    def test_disabled_publish_fails_before_evidence_or_write(self):
        with patch.object(m.legacy.checks,'api',side_effect=AssertionError('No API')),patch.object(m,'write_api',side_effect=AssertionError('No writes')):
            with self.assertRaises(ValueError):m.publish('100',None,'absent',self.release_id,CTX)

    def test_policy_rejects_unsupported_scope_or_activation_without_adoption(self):
        for change in [{'target':'production-ecs'},{'branch':'main'},{'deliveryUnits':['frontend','auth-api']},{'deploymentEnabled':True},{'unexpected':True},{'adoptionReviewed':'true'}]:
            with self.subTest(change=change),patch.object(m.POLICY.__class__,'read_text',return_value=json.dumps(POLICY|change)),self.assertRaises(ValueError):m.read_policy()

    def test_prepare_uses_existing_candidate_validator_and_records_no_deployment(self):
        with patch.object(m,'current_ref',return_value='absent'),patch.object(m.legacy,'prepare',return_value=self.candidate) as verify,patch.object(m,'write_api',side_effect=AssertionError('No writes')):
            files,report=m.prepare('100',None,'absent')
        verify.assert_called_once_with(pr_number='100')
        self.assertEqual(files,self.bundle);self.assertEqual(report['status'],'disabled');self.assertFalse(report['published']);self.assertFalse(report['deployed'])

    def test_superseded_unverified_or_wrong_image_rejected(self):
        for change in [{'candidateVerified':False},{'status':'superseded'},{'images':{'frontend':'docker.io/evil/image@sha256:'+'a'*64}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):m.build(self.candidate|change)

    def test_modified_contents_or_unapproved_paths_rejected(self):
        for name,value in [('values/frontend.json',b'{}'),('.github/workflows/evil.yml',b'on: push'),('../leak',b'bad'),('charts/apps/../escape',b'bad')]:
            files=dict(self.bundle);files[name]=value
            with self.subTest(name=name),self.assertRaises(ValueError):m.validate_bundle(files)

    def test_rehashed_extra_resource_or_secret_is_still_rejected(self):
        for kind in ['Secret','PersistentVolumeClaim']:
            files=dict(self.bundle);files['charts/apps/templates/unapproved.yaml']=('apiVersion: v1\nkind: '+kind+'\nmetadata: {name: bad, namespace: frontend}\n').encode()
            meta=json.loads(files['release.json']);meta['files']={n:m.digest(v)for n,v in sorted(files.items())if n!='release.json'};meta.pop('releaseId');meta['releaseId']=m.digest(m.canonical(meta));files['release.json']=m.canonical(meta)
            with self.subTest(kind=kind),self.assertRaises(ValueError):m.validate_bundle(files)

    def test_changed_ref_is_rejected_before_candidate_build(self):
        with patch.object(m,'current_ref',return_value='d'*40),patch.object(m.legacy,'prepare',side_effect=AssertionError('No stale build')):
            with self.assertRaises(ValueError):m.prepare('100',None,'absent')

    def api(self,change=None):
        responses={
            f'repos/{m.CLOUD}/commits/main':{'sha':'a'*40},
            f'repos/{m.CLOUD}/environments/{m.ENVIRONMENT}':{'id':9,'protection_rules':[{'type':'required_reviewers','reviewers':[{'type':'User','reviewer':{'login':'operator'}}]}],'deployment_branch_policy':{'protected_branches':False,'custom_branch_policies':True}},
            f'repos/{m.CLOUD}/environments/{m.ENVIRONMENT}/deployment-branch-policies':{'branch_policies':[{'name':'main','type':'branch'}]},
            f'repos/{m.CLOUD}/rules/branches/deploy%2Fv3-approved':[{'type':x}for x in ['update','deletion','non_fast_forward']],
            f'repos/{m.CLOUD}/actions/runs/123/approvals':[{'state':'approved','environments':[{'id':9,'name':m.ENVIRONMENT}]}]}
        responses.update(change or {})
        return lambda path:deepcopy(responses[path])

    def test_manual_main_first_attempt_and_actual_approval_are_required(self):
        with patch.object(m.legacy.checks,'api',side_effect=self.api()):m.verify_approval(CTX)
        for change in [{'GITHUB_REF':'refs/heads/work'},{'GITHUB_EVENT_NAME':'push'},{'GITHUB_RUN_ATTEMPT':'2'},{'GITHUB_REPOSITORY':'foreign/repo'},{'GITHUB_ACTIONS':'false'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):m.verify_approval(CTX|change)

    def test_missing_reviewer_main_policy_rules_or_current_approval_rejected(self):
        envpath=f'repos/{m.CLOUD}/environments/{m.ENVIRONMENT}'
        changes=[{envpath:{'id':9,'protection_rules':[]}},{f'repos/{m.CLOUD}/commits/main':{'sha':'b'*40}},
            {envpath+'/deployment-branch-policies':{'branch_policies':[{'name':'*','type':'branch'}]}},
            {f'repos/{m.CLOUD}/rules/branches/deploy%2Fv3-approved':[]},
            {f'repos/{m.CLOUD}/actions/runs/123/approvals':[]},
            {f'repos/{m.CLOUD}/actions/runs/123/approvals':[{'state':'rejected','environments':[{'id':9,'name':m.ENVIRONMENT}]}]}]
        for change in changes:
            with self.subTest(change=change),patch.object(m.legacy.checks,'api',side_effect=self.api(change)),self.assertRaises(ValueError):m.verify_approval(CTX)

    def publication(self,previous='d'*40,preview=None,ref_values=None,write=None):
        report={'releaseId':self.release_id,'published':False,'deployed':False,'action':'deploy'}
        policy=POLICY|{'deploymentEnabled':True,'adoptionReviewed':True}
        calls=[]
        def writer(path,method,payload):
            calls.append((path,method,payload))
            if write:return write(path,method,payload)
            return {'sha':'b'*40}
        with patch.object(m,'read_policy',return_value=policy),patch.object(m.legacy.checks,'api',side_effect=self.api()),patch.object(m,'prepare',return_value=(self.bundle,report)),patch.object(m,'current_ref',side_effect=ref_values or [previous]),patch.object(m,'write_api',side_effect=writer):
            result=m.publish('100',None,previous,preview or self.release_id,CTX)
        return result,calls

    def test_publication_updates_only_approved_ref_without_force(self):
        result,calls=self.publication()
        self.assertTrue(result['published']);self.assertFalse(result['deployed'])
        self.assertEqual(calls[-1],(f'repos/{m.CLOUD}/git/refs/heads/{m.BRANCH}','PATCH',{'sha':'b'*40,'force':False}))
        commit=next(payload for path,method,payload in calls if path.endswith('/commits'))
        self.assertEqual(commit['parents'],['d'*40])
        tree=next(payload for path,method,payload in calls if path.endswith('/trees'))
        self.assertNotIn('base_tree',tree)
        self.assertEqual({x['path']for x in tree['tree']},set(self.bundle))

    def test_first_publication_creates_ref_instead_of_overwriting(self):
        _,calls=self.publication(previous='absent')
        self.assertEqual(calls[-1],(f'repos/{m.CLOUD}/git/refs','POST',{'ref':'refs/heads/'+m.BRANCH,'sha':'b'*40}))

    def test_preview_mismatch_or_concurrent_ref_blocks_writes(self):
        for kwargs in [{'preview':'f'*64},{'ref_values':['e'*40]}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.publication(**kwargs)

    def test_ref_conflict_is_not_retried_or_forced(self):
        def conflict(path,method,payload):
            if '/git/refs/'in path:raise subprocess.CalledProcessError(1,['gh'])
            return {'sha':'b'*40}
        with self.assertRaises(subprocess.CalledProcessError):self.publication(write=conflict)

    def test_rollback_requires_distinct_ancestor_and_valid_bundle(self):
        with patch.object(m,'git',return_value='') as git,patch.object(m,'source_files',return_value=self.bundle):
            self.assertEqual(m.rollback_bundle('b'*40,'d'*40),self.bundle)
            git.assert_called_once_with('merge-base','--is-ancestor','b'*40,'d'*40)
        with self.assertRaises(ValueError):m.rollback_bundle('d'*40,'d'*40)
        with patch.object(m,'git',side_effect=subprocess.CalledProcessError(1,['git'])):
            with self.assertRaises(subprocess.CalledProcessError):m.rollback_bundle('b'*40,'d'*40)

    def test_real_git_snapshot_rollback_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory(prefix='gitops-history-') as temp:
            root=Path(temp)
            def git(*args):
                return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false',*args],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
            git('init')
            for name,data in self.bundle.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            git('add','.');git('commit','-m','approved release')
            first=git('rev-parse','HEAD')
            git('commit','--allow-empty','-m','next approved release');second=git('rev-parse','HEAD')
            with patch.object(m,'ROOT',root):
                self.assertEqual(m.rollback_bundle(first,second),self.bundle)
            (root/'charts/apps/symlink').symlink_to('/tmp/untrusted-target')
            git('add','.');git('commit','-m','invalid snapshot');third=git('rev-parse','HEAD')
            with patch.object(m,'ROOT',root),self.assertRaises(ValueError):m.source_files(third,'charts/apps')
