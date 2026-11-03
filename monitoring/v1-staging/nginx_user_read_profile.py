"""Explicit V1 user-limiter profile; the legacy renderer remains protected by default."""
from nginx_read_enforcement import ENFORCED_PREFIX, ENFORCED_SERVER
from nginx_create_enforcement import ENFORCED_LOCATION

BACKEND_DIGEST = 'sha256:3f1cb707caa276a7b834d92279d3738184f0b7c8b681e876544f0cc9612dc2b8'
IP_RATE = 25
IP_BURST = 30


def user_read_profile(site, backend_digest):
    if backend_digest != BACKEND_DIGEST:
        raise ValueError('The reviewed V1 user-limiter image digest is required')
    if (site.count(ENFORCED_PREFIX) != 1 or site.count(ENFORCED_SERVER) != 1
            or site.count(ENFORCED_LOCATION) != 1):
        raise ValueError('Expected exact V1 staging read and creation policies')
    updated = ENFORCED_PREFIX.replace('rate=5r/s;', f'rate={IP_RATE}r/s;')
    return site.replace(ENFORCED_PREFIX, updated, 1)
