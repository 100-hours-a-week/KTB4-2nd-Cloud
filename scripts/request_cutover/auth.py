"""Pinned BE 05730cbb DB -> Redis representation; no token values are logged."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Entry:
    kind: str
    value: object
    expires: int


TABLES = {
    'sessions': '''SELECT s.sid,s.user_id,s.refresh_token_hash,s.expires_at
        FROM login_sessions s JOIN users u ON u.user_id=s.user_id
        WHERE u.deleted_at IS NULL''',
    'csrf': 'SELECT browser_context_hash,token_value,expires_at FROM csrf_tokens',
    'state': 'SELECT state_hash,browser_context_hash,expires_at FROM oauth_states',
    'ticket': '''SELECT ticket_hash,browser_context_hash,provider_user_id,email,
        profile_image_url,expires_at FROM login_tickets''',
    'profile': '''SELECT token_hash,provider_user_id,email,profile_image_url,expires_at
        FROM profile_tokens''',
}


def expiry(value, source_zone):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(source_zone))
    return int(value.timestamp() * 1000)


def project(rows, prefix, zone, now_ms, owner):
    result = {}

    def put(key, kind, value, end):
        if key in result:
            raise ValueError('duplicate authentication key; refuse migration')
        result[key] = Entry(kind, value, end)

    indexes = {}
    for row in rows['sessions']:
        end = expiry(row['expires_at'], zone)
        if end <= now_ms:
            continue
        sid, uid, rh = row['sid'], str(row['user_id']), row['refresh_token_hash']
        if not sid or not rh or int(uid) <= 0:
            raise ValueError('invalid source session')
        put(prefix + 'session:' + sid, 'hash', {
            'userId': uid, 'refreshTokenHash': rh, 'expiresAt': str(end)}, end)
        put(prefix + 'refresh:' + rh, 'string', sid, end)
        indexes.setdefault(uid, {})[sid] = end
    for uid, members in indexes.items():
        put(prefix + 'user-sessions:' + uid, 'zset', members, max(members.values()))
    for row in rows['csrf']:
        end = expiry(row['expires_at'], zone)
        if end > now_ms:
            put(prefix + 'csrf:' + row['browser_context_hash'], 'string', row['token_value'], end)
    for row in rows['state']:
        end = expiry(row['expires_at'], zone)
        if end > now_ms:
            put(prefix + 'oauth-state:' + row['state_hash'], 'string', row['browser_context_hash'], end)
    for table, key_part, hash_field in (
            ('ticket', 'login-ticket:', 'ticket_hash'), ('profile', 'profile-token:', 'token_hash')):
        for row in rows[table]:
            end = expiry(row['expires_at'], zone)
            if end <= now_ms:
                continue
            identity = {k: row[f] for k, f in (
                ('providerUserId', 'provider_user_id'), ('email', 'email'),
                ('profileImageUrl', 'profile_image_url'))}
            value = json.dumps({'identity': identity,
                'browserContextHash': row.get('browser_context_hash'),
                'expiresAt': end, 'claimOwner': None, 'issuanceOwner': owner},
                separators=(',', ':'), ensure_ascii=False)
            put(prefix + key_part + row[hash_field], 'string', value, end)
    return result


def snapshot(connection):
    """Consistent read, including empty tables; caller must fence all HTTP writers."""
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        try:
            cursor.execute('SELECT CURRENT_TIMESTAMP(6) AS source_now')
            source_now = cursor.fetchone()['source_now']
            rows = {}
            for key, sql in TABLES.items():
                cursor.execute(sql)
                rows[key] = cursor.fetchall()
        finally:
            connection.rollback()
    return rows, source_now


def fingerprint(entries):
    payload = [[k, e.kind, e.value, e.expires] for k, e in sorted(entries.items())]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def replace_inactive(redis, entries, prefix):
    """This run's inactive namespace only. Deletions/consumed tokens are reconciled."""
    keys = list(redis.scan_iter(match=prefix + '*', count=500))
    for start in range(0, len(keys), 500):
        redis.delete(*keys[start:start + 500])
    # Small transactions bound memory; target is still inactive, not a distributed transaction.
    items = list(entries.items())
    for start in range(0, len(items), 100):
        with redis.pipeline(transaction=True) as pipe:
            for key, entry in items[start:start + 100]:
                if entry.kind == 'hash':
                    pipe.hset(key, mapping=entry.value)
                elif entry.kind == 'zset':
                    pipe.zadd(key, entry.value)
                else:
                    pipe.set(key, entry.value)
                pipe.pexpireat(key, entry.expires)
            pipe.execute()


def verify(redis, entries, prefix, now_ms):
    expected = {k: v for k, v in entries.items() if v.expires > now_ms}
    actual = {}
    keys = list(redis.scan_iter(match=prefix + '*', count=500))
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        with redis.pipeline(transaction=False) as pipe:
            for key in batch:
                pipe.type(key)
                pipe.pexpiretime(key)
            metadata = pipe.execute()
        live = []
        for index, key in enumerate(batch):
            kind, end = metadata[index * 2:index * 2 + 2]
            if kind == 'none' or end <= now_ms:
                continue
            if kind not in ('hash', 'zset', 'string'):
                raise ValueError('unexpected target key type')
            live.append((key, kind, end))
        with redis.pipeline(transaction=False) as pipe:
            for key, kind, _ in live:
                if kind == 'hash':
                    pipe.hgetall(key)
                elif kind == 'zset':
                    pipe.zrange(key, 0, -1, withscores=True)
                else:
                    pipe.get(key)
            values = pipe.execute()
        for (key, kind, end), value in zip(live, values):
            if kind == 'zset':
                value = {member: int(score) for member, score in value}
            actual[key] = Entry(kind, value, end)
    if actual != expected:
        raise ValueError('Redis fields, deletion reconciliation, index or expiration mismatch')
    return {'keys': len(actual), 'fingerprint': fingerprint(actual)}
