#!/usr/bin/env python3
"""Prepare and remove V1 staging MySQL-backed sessions for migration tests."""

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import UUID, uuid4


SCHEMA = "yeodam_v1_source"
BASE_URL = "https://v1-staging.yeodam-2gether.com"
SECRET_PARAMETER = "/yeodam/v2/staging/v1-source/app/JWT_SECRET"
ISSUER_PARAMETER = "/yeodam/v2/staging/v1-source/app/JWT_ISSUER"
AUDIENCE = "yeodam-api"
TOKEN_TTL = timedelta(minutes=30)  # V1 deployed BE 5b89f822 application.yaml


def sql_literal(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9@.%-]+", value):
        raise ValueError("Unexpected SQL value")
    return "'" + value + "'"


def run_mysql(statement):
    process = subprocess.run(
        ["mysql", "--protocol=socket", "--user=root", "--database=" + SCHEMA,
         "--batch", "--raw", "--skip-column-names"],
        input=statement, text=True, capture_output=True, check=False,
    )
    if process.returncode:
        raise RuntimeError("MySQL command failed; check local MySQL and schema. Details are not printed to avoid leaking data.")
    return process.stdout.strip()


def parameter(name):
    process = subprocess.run(
        ["aws", "ssm", "get-parameter", "--region", "ap-northeast-2", "--name", name,
         "--with-decryption", "--query", "Parameter.Value", "--output", "text", "--no-cli-pager"],
        text=True, capture_output=True, check=False,
    )
    if process.returncode or not process.stdout.strip():
        raise RuntimeError("Staging JWT parameter could not be read. Check App role and SSM parameter path.")
    return process.stdout.strip()


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def access_token(user_id, sid, secret, issuer, now):
    header = b64url(b'{"alg":"HS256"}')
    claims = {
        "iss": issuer,
        "aud": [AUDIENCE],
        "iat": int(now.timestamp()),
        "exp": int((now + TOKEN_TTL).timestamp()),
        "sub": str(user_id),
        "sid": sid,
    }
    body = b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=True).encode())
    signing_input = (header + "." + body).encode("ascii")
    signature = b64url(hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest())
    return header + "." + body + "." + signature


def http_session(token, sid):
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    # Cookie is set explicitly so /auth/csrf uses this known UUID context.
    cookies = "accessToken=" + token + "; CSRF_CONTEXT=" + sid
    request = Request(BASE_URL + "/api/auth/csrf", headers={"Cookie": cookies})
    with opener.open(request, timeout=15) as response:
        csrf = json.load(response)["data"]["token"]
    if not isinstance(csrf, str) or not csrf:
        raise RuntimeError("CSRF endpoint did not return a token")
    request = Request(BASE_URL + "/api/users/me", headers={"Cookie": cookies})
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("V1 authentication smoke failed")
    return csrf


def run_uuid(value):
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("run-id must be a canonical lowercase UUID")
    return value


def user_prefix(run_id):
    return "mig-" + run_id + "-"


def seed_email(dataset_id, index):
    return "mig-data-" + dataset_id.replace("-", "") + "-" + str(index).zfill(5) + "@staging.invalid"


def save_existing_pool(output, run_id, dataset_id, issued_at, sessions):
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.stat().st_mode & 0o077:
        raise RuntimeError("Output directory must not be readable by group or others")
    descriptor, temporary = tempfile.mkstemp(prefix=".sessions-", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "mode": "existing", "runId": run_id,
                       "datasetId": dataset_id, "baseUrl": BASE_URL,
                       "expiresAt": (issued_at + TOKEN_TTL).isoformat(),
                       "sessions": sessions}, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_existing(args):
    if os.geteuid() != 0:
        raise RuntimeError("Run on the V1 staging App as root")
    if not 1 <= args.count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    output = Path(args.output)
    if output.exists():
        raise RuntimeError("Output already exists; refuse to overwrite session credentials")
    if run_mysql("SELECT DATABASE(), @@hostname;").split("\t")[0] != SCHEMA:
        raise RuntimeError("Unexpected MySQL schema")
    if run_mysql("SELECT @@read_only, @@super_read_only;") != "0\t0":
        raise RuntimeError("Source is not the writable V1 staging DB")
    secret = parameter(SECRET_PARAMETER)
    issuer = parameter(ISSUER_PARAMETER)
    if len(secret.encode("utf-8")) < 32 or not issuer.startswith("https://"):
        raise RuntimeError("Unexpected V1 JWT secret or issuer")
    sessions = []
    first_issued_at = None
    try:
        for index in range(args.count):
            email = seed_email(args.dataset_id, index)
            rows = run_mysql("SELECT user_id FROM users WHERE email=" + sql_literal(email) + ";").splitlines()
            if len(rows) != 1:
                raise RuntimeError("Expected exactly one preloaded user at seed index " + str(index))
            user_id = int(rows[0])
            sid = str(uuid4())
            # Keep the real refresh credential in the private pool so a long
            # migration run can rotate through the deployed auth endpoint.
            refresh_token = secrets.token_urlsafe(32)
            refresh_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
            now = datetime.now(timezone.utc)
            if first_issued_at is None:
                first_issued_at = now
            token = access_token(user_id, sid, secret, issuer, now)
            # Persist the SID before inserting it; a later failure is cleanable.
            sessions.append({"userId": user_id, "seedIndex": index, "sid": sid,
                             "accessToken": token, "refreshToken": refresh_token,
                             "csrfContext": sid, "csrfToken": None})
            save_existing_pool(output, args.run_id, args.dataset_id, first_issued_at, sessions)
            run_mysql("INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ("
                      + sql_literal(sid) + "," + str(user_id) + "," + sql_literal(refresh_hash)
                      + ",DATE_ADD(NOW(6), INTERVAL 7 DAY));")
            sessions[-1]["csrfToken"] = http_session(token, sid)
            save_existing_pool(output, args.run_id, args.dataset_id, first_issued_at, sessions)
    except Exception:
        print("Preparation failed; clean partial sessions using cleanup-existing and this pool file.", file=sys.stderr)
        raise
    print(f"Prepared {len(sessions)} existing-user V1 staging sessions; credentials: {output} (mode 600)")


def cleanup_existing(args):
    if os.geteuid() != 0:
        raise RuntimeError("Run on the V1 staging App as root")
    output = Path(args.output)
    if not output.is_file() or output.stat().st_mode & 0o077:
        raise RuntimeError("Private existing-user pool file is required for scoped cleanup")
    pool = json.loads(output.read_text(encoding="utf-8"))
    if pool.get("mode") != "existing" or pool.get("runId") != args.run_id or pool.get("datasetId") != args.dataset_id:
        raise RuntimeError("Pool identity differs; no sessions were deleted")
    removed = 0
    for session in pool["sessions"]:
        user_id, index, sid = session["userId"], session["seedIndex"], session["sid"]
        if str(UUID(sid)) != sid or not isinstance(user_id, int) or not isinstance(index, int):
            raise ValueError("Invalid pool session identity")
        email = seed_email(args.dataset_id, index)
        match = run_mysql("SELECT COUNT(*) FROM login_sessions s JOIN users u ON u.user_id=s.user_id "
                          "WHERE s.sid=" + sql_literal(sid) + " AND s.user_id=" + str(user_id)
                          + " AND u.email=" + sql_literal(email) + ";")
        if match not in ("0", "1"):
            raise RuntimeError("Session identity is not unique")
        if match == "1":
            context_hash = hashlib.sha256(sid.encode("ascii")).hexdigest()
            run_mysql("DELETE FROM csrf_tokens WHERE browser_context_hash=" + sql_literal(context_hash) + ";")
            run_mysql("DELETE FROM login_sessions WHERE sid=" + sql_literal(sid)
                      + " AND user_id=" + str(user_id) + ";")
            removed += 1
    output.unlink()
    print(f"Removed only {removed} run-scoped sessions/CSRF rows; preloaded users and trips remain")


def prepare(args):
    if os.geteuid() != 0:
        raise RuntimeError("Run on the V1 staging App as root for MySQL socket and private output permissions")
    if not 1 <= args.count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    output = Path(args.output)
    if output.exists():
        raise RuntimeError("Output already exists; refuse to overwrite session credentials")
    if run_mysql("SELECT DATABASE(), @@hostname;").split("\t")[0] != SCHEMA:
        raise RuntimeError("Unexpected MySQL schema")
    if run_mysql("SELECT COUNT(*) FROM users WHERE email LIKE " + sql_literal(user_prefix(args.run_id) + "%@staging.invalid") + ";") != "0":
        raise RuntimeError("This run ID already has users; clean it before retrying")

    secret = parameter(SECRET_PARAMETER)
    issuer = parameter(ISSUER_PARAMETER)
    if len(secret.encode("utf-8")) < 32 or not issuer.startswith("https://"):
        raise RuntimeError("Unexpected V1 JWT secret or issuer")
    sessions = []
    first_issued_at = None
    try:
        for index in range(args.count):
            sid = str(uuid4())
            email = user_prefix(args.run_id) + str(index).zfill(4) + "@staging.invalid"
            refresh_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            sql = (
                "START TRANSACTION;"
                "INSERT INTO users (email,nickname) VALUES (" + sql_literal(email) + ",'test');"
                "SET @synthetic_user_id=LAST_INSERT_ID();"
                "INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ("
                + sql_literal(sid) + ",@synthetic_user_id," + sql_literal(refresh_hash)
                + ",DATE_ADD(NOW(6), INTERVAL 7 DAY));"
                "COMMIT; SELECT @synthetic_user_id;"
            )
            user_id = int(run_mysql(sql).splitlines()[-1])
            now = datetime.now(timezone.utc)
            if first_issued_at is None:
                first_issued_at = now
            token = access_token(user_id, sid, secret, issuer, now)
            csrf = http_session(token, sid)
            sessions.append({"userId": user_id, "sid": sid, "accessToken": token,
                             "csrfContext": sid, "csrfToken": csrf})
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.parent.stat().st_mode & 0o077:
            raise RuntimeError("Output directory must not be readable by group or others")
        descriptor, temporary = tempfile.mkstemp(prefix=".sessions-", dir=output.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "runId": args.run_id, "baseUrl": BASE_URL,
                           "expiresAt": (first_issued_at + TOKEN_TTL).isoformat(),
                           "sessions": sessions}, stream, separators=(",", ":"))
                stream.write("\n")
            os.link(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f"Prepared {len(sessions)} V1 staging sessions; credentials: {output} (mode 600)")
    except Exception:
        print("Preparation failed; run cleanup with the same run ID before retrying.", file=sys.stderr)
        raise


def cleanup(args):
    if os.geteuid() != 0:
        raise RuntimeError("Run on the V1 staging App as root")
    if args.output:
        output = Path(args.output)
        if output.exists():
            pool = json.loads(output.read_text(encoding="utf-8"))
            if pool.get("runId") != args.run_id:
                raise RuntimeError("Output file belongs to a different run; nothing was deleted")
            if pool.get("mode") == "existing":
                raise RuntimeError("Existing-user pool requires cleanup-existing; nothing was deleted")
    # Session IDs are selected only from users under this exact synthetic run prefix.
    users = "SELECT user_id FROM users WHERE email LIKE " + sql_literal(user_prefix(args.run_id) + "%@staging.invalid")
    sid_rows = run_mysql("SELECT sid FROM login_sessions WHERE user_id IN (" + users + ");")
    for sid in sid_rows.splitlines():
        context_hash = hashlib.sha256(sid.encode("ascii")).hexdigest()
        run_mysql("DELETE FROM csrf_tokens WHERE browser_context_hash=" + sql_literal(context_hash) + ";")
    count = run_mysql("SELECT COUNT(*) FROM users WHERE email LIKE " + sql_literal(user_prefix(args.run_id) + "%@staging.invalid") + ";")
    run_mysql("DELETE FROM login_sessions WHERE user_id IN (" + users + ");")
    run_mysql("DELETE FROM users WHERE email LIKE " + sql_literal(user_prefix(args.run_id) + "%@staging.invalid") + ";")
    if args.output:
        if output.exists():
            output.unlink()
    print(f"Removed {count} synthetic users and related sessions/CSRF rows for run {args.run_id}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "cleanup", "prepare-existing", "cleanup-existing"):
        sub = subcommands.add_parser(action)
        sub.add_argument("--run-id", required=True, type=run_uuid)
        sub.add_argument("--output", required=action in ("prepare", "prepare-existing", "cleanup-existing"))
        if action in ("prepare", "prepare-existing"):
            sub.add_argument("--count", type=int, default=100)
        if action in ("prepare-existing", "cleanup-existing"):
            sub.add_argument("--dataset-id", required=True, type=run_uuid)
    args = parser.parse_args()
    try:
        {"prepare": prepare, "cleanup": cleanup,
         "prepare-existing": prepare_existing, "cleanup-existing": cleanup_existing}[args.action](args)
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
