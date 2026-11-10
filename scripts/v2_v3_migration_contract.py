"""Offline metadata checks only; no AWS, MySQL clients or migration execution."""
import hashlib
import json
from pathlib import Path
import re
import zlib
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / 'deploy/v3/mysql-schema-image/source.lock.json').read_text())
SOURCE_BUCKET = 'yeodam-v1-app-data-483175530259-ap-northeast-2'
TARGET_BUCKET = 'yeodam-v3-foundation-app-data-483175530259-ap-northeast-2'
BINDINGS = {'sourceMysqlInstance': 'i-02b2db8886a13dc25',
            'targetMysqlService': 'mysql-primary.mysql.svc.cluster.local',
            'sourceBucket': SOURCE_BUCKET, 'targetBucket': TARGET_BUCKET,
            'protectedGpu': 'i-05e1ad9dd96dba4ae'}
STRATEGIES = {'s3': {'copy-same-keys'}, 'redis': {'fresh-login', 'selective-transfer'},
              'aiRedis': {'drain', 'reconcile-pending'}, 'kafka': {'reconcile-business-no-offset-copy'},
              'qdrant': {'snapshot', 'reindex'}, 'mongodb': {'no-v2-source', 'migrate-existing'}}
FIELDS = {'meta': {'kind','schema','engine','serverUuid','capturedAt','readOnly'},
          'history': {'kind','rank','version','script','checksum','success'},
          'table': {'kind','table','estimatedRows','dataBytes','indexBytes'},
          'column': {'kind','table','column','position','type','nullable','collation'},
          'foreignKey': {'kind','table','constraint','column','position','parentTable','parentColumn'},
          'count': {'kind','table','rows'}}
NAME = re.compile(r'[a-z_][a-z0-9_]{0,63}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    return type(value) is int and value >= 0


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value), 'Invalid UTC evidence time')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def fresh(value, now):
    return 0 <= (now - timestamp(value)).total_seconds() <= 3600


def load_records(path):
    require(not path.is_symlink() and path.stat().st_size <= 10 * 1024 * 1024, 'Invalid metadata file')
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    require(rows and all(isinstance(r, dict) and r.get('kind') in FIELDS
                        and set(r) == FIELDS[r['kind']] for r in rows), 'Unexpected metadata fields')
    return rows


def flyway_crc(data):
    # Flyway 12.4.0 ChecksumCalculator: first-line BOM removal, UTF-8 lines, no separators.
    # https://github.com/flyway/flyway/blob/flyway-12.4.0/flyway-core/src/main/java/org/flywaydb/core/internal/resolver/ChecksumCalculator.java
    lines = re.split(r'\r\n|\r|\n', data.decode('utf-8'))
    lines[0] = lines[0].removeprefix('\ufeff')
    crc = 0
    for line in lines:
        crc = zlib.crc32(line.encode('utf-8'), crc)
    return crc if crc < 2**31 else crc - 2**32


def sql_checksums(directory, lock=LOCK):
    require(not directory.is_symlink(), 'Symlink SQL directory rejected')
    expected = {x['name'] for x in lock['migrations']}
    require({p.name for p in directory.iterdir()} == expected, 'SQL file set differs from pinned schema source')
    result = {}
    for item in lock['migrations']:
        path = directory / item['name']
        require(path.is_file() and not path.is_symlink(), 'Invalid SQL file')
        data = path.read_bytes()
        require(len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256'], 'SQL source hash mismatch')
        version = int(re.fullmatch(r'V(\d+)__[a-z0-9_]+\.sql', item['name'])[1])
        result[version] = {'script': item['name'], 'checksum': flyway_crc(data)}
    require(set(result) == set(range(1, lock['schemaVersion'] + 1)), 'Pinned migration versions must be contiguous')
    return result


def schema(rows, expected, now):
    require(all(r['kind'] != 'count' for r in rows), 'Count evidence must be separate')
    metas = [r for r in rows if r['kind'] == 'meta']
    require(len(metas) == 1, 'Exactly one metadata header required')
    meta = metas[0]
    require(meta['schema'] == 'yeodam' and isinstance(meta['engine'], str)
            and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', meta['engine'])
            and re.fullmatch(r'[0-9a-f-]{36}', meta['serverUuid']) and type(meta['readOnly']) is int
            and meta['readOnly'] in [0, 1] and fresh(meta['capturedAt'], now), 'Wrong or stale MySQL metadata')
    history = sorted([r for r in rows if r['kind'] == 'history'], key=lambda r: r['rank'])
    require(history and all(type(r['rank']) is int and r['rank'] > 0 and type(r['version']) is str
                           and re.fullmatch(r'[1-9]\d*', r['version']) for r in history), 'Invalid Flyway history')
    versions = [int(r['version']) for r in history]
    require(len({r['rank'] for r in history}) == len(history)
            and versions == list(range(1, len(history) + 1)) and set(versions) <= set(expected),
            'History must be a complete successful prefix in installed-rank order')
    for row in history:
        wanted = expected[int(row['version'])]
        require(type(row['success']) is int and row['success'] == 1 and type(row['checksum']) is int
                and row['script'] == wanted['script'] and row['checksum'] == wanted['checksum'],
                'Flyway script or checksum mismatch; no repair/baseline permitted')
    tables = [r for r in rows if r['kind'] == 'table']
    names = {r['table'] for r in tables}
    require(tables and len(names) == len(tables) and 'flyway_schema_history' in names
            and all(NAME.fullmatch(r['table']) and all(number(r[k]) for k in ['estimatedRows','dataBytes','indexBytes']) for r in tables), 'Invalid table catalog')
    columns = [r for r in rows if r['kind'] == 'column']
    require(columns and all(r['table'] in names and NAME.fullmatch(r['column'])
                            and number(r['position']) and r['position'] > 0 for r in columns)
            and {r['table'] for r in columns} == names
            and len({(r['table'], r['column']) for r in columns}) == len(columns), 'Incomplete column catalog')
    structural = [r for r in rows if r['kind'] in ['column','foreignKey']]
    require(all(r['table'] in names and r['parentTable'] in names for r in structural if r['kind'] == 'foreignKey'), 'Incomplete FK catalog')
    fingerprint = hashlib.sha256(json.dumps(sorted(structural, key=lambda r: json.dumps(r,sort_keys=True)), sort_keys=True).encode()).hexdigest()
    return {'latestVersion': versions[-1], 'tableCount': len(tables),
            'estimatedStorageBytes': sum(r['dataBytes'] + r['indexBytes'] for r in tables),
            'schemaFingerprint': fingerprint, 'serverUuid': meta['serverUuid'], 'engine': meta['engine']}


def repository_bindings():
    """Check public deployment references; Secret values and Terraform state are never inputs."""
    connection = json.loads((ROOT/'deploy/v3/apps/files/connection-contract.json').read_text())['commonConfig']
    # Public contracts currently contain connection values directly.
    require(connection['ATTACHMENT_S3_BUCKET'] == TARGET_BUCKET
            and connection['MYSQL_URL'].startswith('jdbc:mysql://' + BINDINGS['targetMysqlService'] + ':3306/yeodam?'),
            'Migration bindings differ from the current BE connection contract')
    ai = json.loads((ROOT/'deploy/v3/ai-apps/files/contract.json').read_text())
    require(ai['commonEnvironment']['QDRANT_URL'] == 'https://qdrant.qdrant.svc.cluster.local:6333'
            and ai['commonEnvironment']['S3_BUCKET'] == TARGET_BUCKET, 'Review changed AI storage endpoints before migration')
    return BINDINGS.copy()


def template():
    return {'capturedAt': None, 'bindings': BINDINGS.copy(),
            'stores': {name: {'strategy': 'pending', 'referencesReviewed': False,
                              'pendingItems': None} for name in STRATEGIES},
            'sourceWritersFrozen': False, 'sourceActiveTransactions': None,
            'mysqlPendingJobs': None, 'qaDataPolicy': 'pending',
            'cdcRestartPlanReviewed': False, 'rollbackAfterNewWritesReviewed': False}


def preflight(source, target, inventory, expected, now):
    blockers = []
    summaries = {}
    for side, rows in [('source', source), ('target', target)]:
        if rows is None:
            blockers.append(side + '_mysql_evidence_missing')
        else:
            try:
                summaries[side] = schema(rows, expected, now)
            except (ValueError, KeyError, TypeError, IndexError):
                blockers.append(side + '_mysql_evidence_invalid')
    if len(summaries) == 2:
        src, dst = summaries['source'], summaries['target']
        if src['serverUuid'] == dst['serverUuid']:
            blockers.append('source_target_are_same_server')
        if src['engine'] != dst['engine']:
            blockers.append('engine_difference_requires_restore_rehearsal')
        if src['latestVersion'] == dst['latestVersion'] and src['schemaFingerprint'] != dst['schemaFingerprint']:
            blockers.append('same_version_schema_drift')
        if dst['latestVersion'] != max(expected):
            blockers.append('target_not_pinned_v35')
    if inventory is not None and not isinstance(inventory, dict):
        blockers.append('storage_inventory_invalid')
        inventory = None
    if inventory is None:
        blockers.append('storage_inventory_missing')
    else:
        if set(inventory) != set(template()) or inventory.get('bindings') != BINDINGS:
            blockers.append('inventory_bindings_or_fields_invalid')
        try:
            if not fresh(inventory.get('capturedAt'), now):
                blockers.append('inventory_stale')
        except (ValueError, TypeError):
            blockers.append('inventory_time_missing')
        if inventory.get('sourceWritersFrozen') is not True or type(inventory.get('sourceActiveTransactions')) is not int or inventory['sourceActiveTransactions'] != 0:
            blockers.append('source_not_frozen')
        if type(inventory.get('mysqlPendingJobs')) is not int or inventory['mysqlPendingJobs'] != 0:
            blockers.append('mysql_pending_jobs_unresolved')
        if inventory.get('qaDataPolicy') not in ['archive-before-replace', 'approved-discard']:
            blockers.append('qa_data_disposition_missing')
        for field in ['cdcRestartPlanReviewed','rollbackAfterNewWritesReviewed']:
            if inventory.get(field) is not True:
                blockers.append(field + '_missing')
        stores = inventory.get('stores', {})
        if not isinstance(stores, dict):
            stores = {}
        if set(stores) != set(STRATEGIES):
            blockers.append('storage_coverage_incomplete')
        for name, strategies in STRATEGIES.items():
            item = stores.get(name, {})
            if not isinstance(item, dict):
                item = {}
            if set(item) != {'strategy','referencesReviewed','pendingItems'} or item.get('strategy') not in strategies or item.get('referencesReviewed') is not True:
                blockers.append(name + '_strategy_unreviewed')
            if not number(item.get('pendingItems')) or item['pendingItems'] != 0:
                blockers.append(name + '_pending_items_unresolved')
    # Never an execution authorization: restore, backup, data checks and cutover still follow.
    return {'metadataChecksPassed': not blockers, 'readyToMigrate': False, 'resourcesChanged': False,
            'blockers': sorted(set(blockers)), 'sourceSchemaVersion': summaries.get('source', {}).get('latestVersion'),
            'targetSchemaVersion': summaries.get('target', {}).get('latestVersion'),
            'pendingMigrationVersions': list(range(summaries['source']['latestVersion'] + 1, max(expected) + 1)) if 'source' in summaries else None,
            'requiredNextSteps': ['backup', 'isolated-restore-rehearsal', 'data-reconciliation', 'approved-cutover'],
            'metadata': {k: {f: v for f,v in s.items() if f != 'serverUuid'} for k,s in summaries.items()}}


def counts_sql(metadata):
    tables = sorted(r['table'] for r in metadata if r['kind'] == 'table')
    require(tables and len(tables) == len(set(tables)) and all(NAME.fullmatch(t) for t in tables), 'Invalid table identifiers')
    return 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;\n' + '\n'.join(
        "SELECT JSON_OBJECT('kind','count','table','" + t + "','rows',COUNT(*)) FROM yeodam.`" + t + '`;'
        for t in tables) + '\nCOMMIT;\n'


def compare_counts(before, after, source_tables, target_tables):
    def index(rows):
        require(rows and all(set(r) == FIELDS['count'] and r['kind'] == 'count' and NAME.fullmatch(r['table']) and number(r['rows']) for r in rows), 'Invalid count evidence')
        require(len({r['table'] for r in rows}) == len(rows), 'Duplicate count table')
        return {r['table']:r['rows'] for r in rows}
    left, right = index(before), index(after)
    require(set(left) == source_tables and set(right) == target_tables, 'Exact counts must cover both full table catalogs')
    # Compare business tables. Flyway history count deliberately grows on upgrade.
    names = set(left) - {'flyway_schema_history'}
    require(names, 'No business table counts')
    missing = names - set(right)
    mismatched = {t for t in names & set(right) if left[t] != right[t]}
    unexpected_nonempty = {t for t in set(right) - set(left) if right[t] != 0}
    return {'rowCountsMatch': not (missing or mismatched or unexpected_nonempty),
            'missingTables': len(missing), 'mismatchedTables': len(mismatched),
            'newNonemptyTablesRequiringReview': len(unexpected_nonempty),
            'comparedBusinessTables': len(names), 'rowContentsVerified': False,
            'readyToMigrate': False, 'resourcesChanged': False}
