#!/usr/bin/env python3
"""Inspect only JWT timestamps before a long test; no signature verification."""
import argparse
import base64
import json
import math
import os
import time
from datetime import datetime, timezone


def inspect(token, minimum, now):
    if minimum < 0:
        raise ValueError('Minimum remaining lifetime must be non-negative')
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1]) % 4)))
        issued, expiry = claims['iat'], claims['exp']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in [issued, expiry]):
            raise ValueError()
        if issued > now + 60 or expiry <= issued:
            raise ValueError()
        remaining = math.floor(expiry-now)
        return {
            'issuedAtUtc': datetime.fromtimestamp(issued, timezone.utc).isoformat(),
            'expiresAtUtc': datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
            'remainingSeconds': remaining,
            'minimumRequiredSeconds': minimum,
            'windowSufficient': remaining > 0 and remaining >= minimum,
            'signatureVerified': False,
        }
    except Exception:
        # Never echo the token, decoded claims, or parse exception payload.
        raise ValueError('Missing or invalid JWT timestamps; token contents are not printed') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--min-remaining-seconds', type=int, required=True)
    args = parser.parse_args()
    try:
        result = inspect(os.environ.get('K6_ACCESS_TOKEN',''), args.min_remaining_seconds, time.time())
    except ValueError as error:
        parser.exit(2, str(error)+'\n')
    print(json.dumps(result))
    return 0 if result['windowSufficient'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
