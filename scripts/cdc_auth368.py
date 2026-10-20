"""Project ordered Debezium auth row changes into an isolated V2 Redis namespace.

This module deliberately has no Kafka transport. A caller must only pass events
from the protected migration auth topic and commit its Kafka offset after the
Redis transaction succeeds. No event values or Redis commands are logged here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import base64
import json

from request_cutover.auth import expiry


TABLES = {"login_sessions", "csrf_tokens"}
OPERATIONS = {"c", "r", "u", "d"}
INDEX_EXPIRY_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local last = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if #last == 0 then
  redis.call('DEL', KEYS[1])
else
  redis.call('PEXPIREAT', KEYS[1], math.floor(tonumber(last[2])))
end
return #last
"""


@dataclass(frozen=True)
class Change:
    table: str
    op: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def _expiry_ms(value: Any, zone: str) -> int:
    # V1 expires_at is DATETIME(6). Debezium's MicroTimestamp is a wall-clock
    # microsecond count without a timezone, not an absolute UTC instant.
    if isinstance(value, int) and not isinstance(value, bool):
        value = datetime(1970, 1, 1) + timedelta(microseconds=value)
    return expiry(value, zone)


def parse_change(envelope: dict[str, Any]) -> Change:
    """Reject DDL/heartbeat/unknown records instead of silently dropping auth data."""
    if not isinstance(envelope, dict):
        raise ValueError("auth event must be an object")
    source = envelope.get("source")
    table = source.get("table") if isinstance(source, dict) else None
    op = envelope.get("op")
    before, after = envelope.get("before"), envelope.get("after")
    if table not in TABLES or op not in OPERATIONS:
        raise ValueError("unexpected auth table or operation")
    if op == "d":
        if not isinstance(before, dict) or after is not None:
            raise ValueError("delete requires before row only")
    elif not isinstance(after, dict):
        raise ValueError("upsert requires after row")
    if before is not None and not isinstance(before, dict):
        raise ValueError("before row must be an object")
    return Change(table, op, before, after)


def _session(row: dict[str, Any]) -> tuple[str, str, str, Any]:
    sid, user_id, refresh_hash, end = (
        row.get("sid"), row.get("user_id"), row.get("refresh_token_hash"),
        row.get("expires_at"),
    )
    if not isinstance(sid, str) or not sid or not isinstance(refresh_hash, str) or not refresh_hash:
        raise ValueError("invalid session identity")
    if not isinstance(user_id, int) or user_id <= 0 or end is None:
        raise ValueError("invalid session user or expiry")
    return sid, str(user_id), refresh_hash, end


def _csrf(row: dict[str, Any]) -> tuple[str, str, Any]:
    context, value, end = row.get("browser_context_hash"), row.get("token_value"), row.get("expires_at")
    if not isinstance(context, str) or not context or not isinstance(value, str) or not value or end is None:
        raise ValueError("invalid CSRF row")
    return context, value, end


def apply_change(redis, envelope: dict[str, Any], prefix: str, zone: str, now_ms: int) -> None:
    """Apply one row event atomically; repeated delivery has the same final state.

    Ordering belongs to the single-partition auth topic. The caller must not
    commit the Kafka offset until this transaction has returned successfully.
    """
    if not prefix.startswith("yeodam:cutover:") or not prefix.endswith(":"):
        raise ValueError("only an isolated migration Redis namespace is allowed")
    change = parse_change(envelope)
    affected_users: set[str] = set()
    with redis.pipeline(transaction=True) as pipe:
        if change.table == "login_sessions":
            if change.before is not None:
                old_sid, old_user, old_hash, _ = _session(change.before)
                affected_users.add(old_user)
                pipe.delete(prefix + "refresh:" + old_hash)
                if change.after is None or change.after.get("sid") != old_sid:
                    pipe.delete(prefix + "session:" + old_sid)
                    pipe.zrem(prefix + "user-sessions:" + old_user, old_sid)
                elif change.after.get("user_id") != change.before.get("user_id"):
                    pipe.zrem(prefix + "user-sessions:" + old_user, old_sid)
            if change.after is not None:
                sid, user_id, refresh_hash, end_value = _session(change.after)
                affected_users.add(user_id)
                end_ms = _expiry_ms(end_value, zone)
                if end_ms <= now_ms:
                    pipe.delete(prefix + "session:" + sid, prefix + "refresh:" + refresh_hash)
                    pipe.zrem(prefix + "user-sessions:" + user_id, sid)
                else:
                    pipe.hset(prefix + "session:" + sid, mapping={
                        "userId": user_id, "refreshTokenHash": refresh_hash,
                        "expiresAt": str(end_ms),
                    })
                    pipe.pexpireat(prefix + "session:" + sid, end_ms)
                    pipe.set(prefix + "refresh:" + refresh_hash, sid, pxat=end_ms)
                    pipe.zadd(prefix + "user-sessions:" + user_id, {sid: end_ms})
        else:
            if change.before is not None:
                old_context, _, _ = _csrf(change.before)
                if change.after is None or change.after.get("browser_context_hash") != old_context:
                    pipe.delete(prefix + "csrf:" + old_context)
            if change.after is not None:
                context, value, end_value = _csrf(change.after)
                end_ms = _expiry_ms(end_value, zone)
                if end_ms <= now_ms:
                    pipe.delete(prefix + "csrf:" + context)
                else:
                    pipe.set(prefix + "csrf:" + context, value, pxat=end_ms)
        pipe.execute()
    # The index expires with its latest active member, not the last event seen.
    # Offset commit follows these calls; a crash replays the idempotent event.
    for user_id in affected_users:
        redis.eval(INDEX_EXPIRY_LUA, 1, prefix + "user-sessions:" + user_id, now_ms)


def decrypt_event(value: bytes, topic: str, key: bytes) -> dict[str, Any]:
    """Unseal the Connect SMT output. Authentication failure stops the consumer."""
    if not topic.startswith("staging.yeodam.migration.auth368.") or len(key) != 32:
        raise ValueError("unexpected auth topic or key length")
    if not value.startswith(b"v1:"):
        raise ValueError("unencrypted auth record refused")
    packed = base64.b64decode(value[3:], validate=True)
    if len(packed) < 29:
        raise ValueError("short auth ciphertext")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    decoded = AESGCM(key).decrypt(packed[:12], packed[12:], topic.encode())
    event = json.loads(decoded)
    if not isinstance(event, dict):
        raise ValueError("invalid auth envelope")
    return event
