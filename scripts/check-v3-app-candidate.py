#!/usr/bin/env python3
"""Inspect disabled BE/AI candidate inputs offline; never activate workloads or read secrets."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'deploy/v3/candidates/be-ai'
ROLES = ('auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker')
AI_URL = 'http://ai-api.ai.svc.cluster.local:8000'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(path.read_text())


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(files, directory):
    require(isinstance(files, dict) and len(files) >= 5, 'Source review file inventory required')
    for name, digest in files.items():
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and
                re.fullmatch('[a-f0-9]{64}', digest), 'Safe source file hashes required')
        if directory:
            require(file_hash(directory / name) == digest, 'Source file differs: ' + name)


def validate(folder=DEFAULT, backend_source=None, ai_source=None):
    candidate = load(folder / 'manifest.json')
    require(set(candidate) == {'schemaVersion', 'kind', 'deploymentEnabled', 'components', 'migration', 'pending'},
            'Exact candidate metadata required')
    require(candidate['schemaVersion'] == 1 and candidate['kind'] == 'v3-be-ai-candidate' and
            candidate['deploymentEnabled'] is False, 'Preparation only; deployment must remain disabled')
    require(set(candidate['components']) == {'backend', 'ai'}, 'Only BE and AI candidates')
    for role, component in candidate['components'].items():
        repo, image, workflow = ('KTB4-2nd-BE', 'yeodam-backend', 'Backend Image') if role == 'backend' else (
            'KTB4-2nd-AI', 'yeodam-ai-app', 'AI v2 App Image')
        require(set(component) == {'sourceRepository', 'sourceSha', 'image', 'platform', 'buildRunId',
                'buildWorkflow', 'buildConclusion', 'digestEvidence', 'registryDigestVerified',
                'runtimeVerified', 'sourceFiles'}, 'Exact public component metadata required')
        require(component['sourceRepository'] == '100-hours-a-week/' + repo and
                re.fullmatch('[a-f0-9]{40}', component['sourceSha']), 'Exact component source required')
        require(re.fullmatch('ghcr.io/100-hours-a-week/' + image + '@sha256:[a-f0-9]{64}', component['image']),
                'Immutable component-specific image required')
        require(component['platform'] == 'linux/amd64' and type(component['buildRunId']) is int and
                component['buildRunId'] > 0 and component['buildWorkflow'] == workflow and
                component['buildConclusion'] == 'success' and component['digestEvidence'] == 'successful-build-log',
                'Successful component image build evidence required')
        require(type(component['registryDigestVerified']) is bool and component['runtimeVerified'] is False,
                'Registry review must be explicit; no runtime proof in offline candidate')
        if component['registryDigestVerified']:
            inspection = load(folder / 'registry-inspection.json')[role]
            require(inspection['image'] == component['image'] and inspection['source'] == component['sourceSha'] and
                    inspection['manifestDigestVerified'] is True and inspection['platform'] == component['platform'] and
                    re.fullmatch('sha256:[a-f0-9]{64}', inspection['configDigest']) and inspection['runtimeVerified'] is False,
                    'Matching immutable registry inspection required')
        source_files(component['sourceFiles'], backend_source if role == 'backend' else ai_source)
    baseline = load(ROOT / 'deploy/v3/mysql-schema-image/source.lock.json')
    migration = candidate['migration']
    require(migration['applied'] is False, 'V35 must remain unapplied')
    require(migration == {**migration, 'baselineSource': baseline['sourceCommit'], 'baselineVersion': 34,
                         'targetVersion': 35, 'baselineMigrations': baseline['migrations'], 'applied': False},
            'Preserve V1-V34 baseline; V35 must remain unapplied')
    require(set(migration) == {'baselineSource', 'baselineVersion', 'targetVersion', 'baselineMigrations',
                               'additionalMigration', 'applied'}, 'Exact migration metadata required')
    addition = migration['additionalMigration']
    require(set(addition) == {'name', 'sha256', 'bytes'} and
            addition['name'] == 'V35__add_story_prepared_content_metadata.sql', 'Only reviewed V35 addition')
    sql = folder / addition['name']
    require(file_hash(sql) == addition['sha256'] and sql.stat().st_size == addition['bytes'],
            'V35 SQL checksum mismatch')
    require(sql.read_text().split() == 'ALTER TABLE story_generation_jobs ADD COLUMN prepared_content_metadata JSON NULL;'.split(),
            'Unexpected V35 SQL operation')
    if backend_source:
        path = backend_source / 'src/main/resources/db/migration'
        source_files({entry['name']: entry['sha256'] for entry in baseline['migrations']}, path)
        require({p.name for p in path.iterdir()} == {e['name'] for e in baseline['migrations']} | {addition['name']},
                'Unreviewed BE migrations')
        require(file_hash(path / addition['name']) == addition['sha256'], 'V35 differs from selected BE source')
    screen = load(folder / 'ai-source-screen.json')
    require(set(screen) == {'kind', 'source', 'status', 'checks', 'missing', 'files', 'runtimeVerified'} and
            screen['kind'] == 'v3-ai-source-screen-v1' and set(screen['checks']) == {
                'kafkaProtocol', 'kafkaSettings', 'kafkaClientOptions', 'qdrantSetting', 'qdrantClients',
                'placeIndexSettings', 'runnerIndexPreparation'}, 'Exact AI source capabilities required')
    require(screen['source'] == candidate['components']['ai']['sourceSha'] and screen['status'] == 'compatible' and
            screen['runtimeVerified'] is False and screen['missing'] == [] and all(screen['checks'].values()) and
            len(screen['checks']) == 7 and len(screen['files']) == 6, 'Source-bound AI screen required')
    if ai_source:
        spec = importlib.util.spec_from_file_location('candidate_source', ROOT / 'scripts/inspect-v3-ai-source.py')
        inspector = importlib.util.module_from_spec(spec); spec.loader.exec_module(inspector)
        require(inspector.inspect({p: (ai_source / p).read_text() for p in inspector.FILES}, screen['source']) == screen,
                'AI source screen differs from selected source files')
    backend = yaml.safe_load((folder / 'backend.values.yaml').read_text())
    expected_backend = {'images': {'backend': candidate['components']['backend']['image']},
        'defaults': {'configMapData': {'RELEASE': candidate['components']['backend']['sourceSha'],
            'AI_SERVER_BASE_URL': AI_URL, 'PHOTO_LIST_CACHE_ENABLED': 'false', 'TRIP_EDIT_LOCK_ENABLED': 'false'}},
        'workloads': {role: {'enabled': False} for role in ROLES}}
    require(all(backend['workloads'][role]['enabled'] is False for role in ROLES), 'BE roles must be disabled')
    require(backend == expected_backend, 'Exact disabled BE overlay required; no secret values or resource overrides')
    ai = yaml.safe_load((folder / 'ai.values.yaml').read_text())
    require(ai['accessEnabled'] is False and all(v is False for v in ai['workloads'].values()) and
            all(v is False for v in ai['review'].values()), 'AI activation and review flags must stay false')
    require(ai == {'accessEnabled': False, 'workloads': {'api': False, 'runner': False},
        'image': candidate['components']['ai']['image'], 'source': candidate['components']['ai']['sourceSha'],
        'review': {'sourceCompatible': False, 'imageReviewed': False, 'connectionsReviewed': False}},
        'Exact disabled AI overlay required; activation reviews stay separate')
    be_base = yaml.safe_load((ROOT / 'deploy/v3/environments/foundation/backend.values.yaml').read_text())
    connection = yaml.safe_load((ROOT / 'deploy/v3/environments/foundation/backend-connections.values.yaml').read_text())
    resources = be_base['defaults']['resources']
    require(resources['requests']['cpu'] == '100m' and resources['requests']['memory'] == '512Mi' and
            resources['limits']['memory'] == '1Gi', 'Keep existing minimum BE budget')
    properties = connection['defaults']['springProperties']['spring']
    require(properties['flyway']['enabled'] is False and properties['jpa']['hibernate']['ddl-auto'] == 'validate',
            'Do not enable startup DB mutation')
    contract = load(ROOT / 'deploy/v3/ai-apps/files/contract.json')
    for role, memory, limit in [('api', '256Mi', '512Mi'), ('runner', '512Mi', '1Gi')]:
        r = contract['workloads'][role]['resources']
        require(r['requests']['cpu'] == '100m' and r['requests']['memory'] == memory and r['limits']['memory'] == limit,
                'Keep existing minimum AI budget')
    require(set(candidate['pending']) == {'registry-image-inspection', 'minimum-memory-runtime-test',
        'v35-backup-migration-and-validation', 'secret-supply-and-ca-review', 'shared-model-route-and-capacity-review',
        'cluster-cold-start-and-dependency-check', 'application-e2e'}, 'Keep explicit activation prerequisites')
    return {'candidatePrepared': True, 'deploymentEnabled': False, 'runtimeVerified': False,
            'backendSource': candidate['components']['backend']['sourceSha'], 'aiSource': screen['source'],
            'backendRoles': list(ROLES), 'backendResourcesPerRole': resources,
            'aiResources': {k: v['resources'] for k, v in contract['workloads'].items()},
            'aiSecretKeys': contract['secretKeys'] + contract['runnerExtraSecrets'],
            'backendSecretReferences': {role: {**be_base['defaults']['secretRefs'],
                **connection['defaults']['secretRefs'], **connection['workloads'][role]['secretRefs']}
                for role in ROLES}, 'pending': candidate['pending']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--backend-source', type=Path)
    parser.add_argument('--ai-source', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.directory, args.backend_source, args.ai_source), indent=2))


if __name__ == '__main__':
    main()
