"""Bounded CI-only public image transport; immutable references only."""
import json
import re
import subprocess
import sys
import time

PRIMARY = 'public.ecr.aws/docker/library/redis'
MIRROR = 'docker.io/library/redis'
REF = re.compile(r'(public[.]ecr[.]aws/docker/library/redis|docker[.]io/library/redis)@(sha256:[a-f0-9]{64})')


def transient(message):
    text = message.lower()
    return any(marker in text for marker in (
        'toomanyrequests', 'too many requests', '429', 'i/o timeout',
        'connection reset', 'connection refused', 'temporary failure in name resolution',
        'no such host', 'tls handshake timeout', 'context deadline exceeded',
        '502 bad gateway', '503 service unavailable', '504 gateway timeout'))


def inspect(reference, run):
    result = run(['docker', 'image', 'inspect', '--platform', 'linux/amd64', reference], capture_output=True, text=True, timeout=30)
    if result.returncode:
        return False
    rows = json.loads(result.stdout)
    expected = REF.fullmatch(reference)
    if len(rows) != 1 or not expected:
        raise ValueError('Unexpected image inspection result')
    row = rows[0]
    acceptable = {reference}
    if expected[1] == MIRROR:
        acceptable.add('redis@' + expected[2])
        acceptable.add('library/redis@' + expected[2])
    if row.get('Os') != 'linux' or row.get('Architecture') != 'amd64' or not acceptable.intersection(row.get('RepoDigests', [])):
        raise ValueError('Downloaded image platform or immutable repository digest differs')
    return True


def pull(reference, run=subprocess.run, sleep=time.sleep):
    match = REF.fullmatch(reference)
    if not match or match[1] != PRIMARY:
        raise ValueError('Only the reviewed immutable Redis source is supported')
    candidates = [reference, MIRROR + '@' + match[2]]
    for candidate in candidates:
        if inspect(candidate, run):
            return candidate
        for attempt in range(3):
            print(f'CI image pull: {candidate} attempt {attempt + 1}/3', file=sys.stderr)
            command = ['docker', 'pull', '--platform', 'linux/amd64', candidate]
            try:
                result = run(command, capture_output=True, text=True, timeout=45)
            except subprocess.TimeoutExpired:
                result = subprocess.CompletedProcess(command, 1, '', 'context deadline exceeded')
            if result.returncode == 0:
                if not inspect(candidate, run):
                    raise ValueError('Pulled image is unavailable for inspection')
                return candidate
            error = result.stdout + result.stderr
            # Auth/not-found/digest failures never become a mirror or retry success.
            if any(word in error.lower() for word in ('unauthorized', 'denied', 'manifest unknown', 'not found', 'digest mismatch')) or not transient(error):
                raise RuntimeError('Image pull failed without a permitted transient classification')
            if attempt < 2:
                sleep((2, 5)[attempt])
    raise RuntimeError('Both immutable image sources exhausted bounded retries')
