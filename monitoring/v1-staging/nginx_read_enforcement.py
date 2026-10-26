"""Promote the validated staging read policy; change no rates, routes or timeouts."""
from nginx_read_dry_run import PREFIX, SERVER, instrument_read_dry_run

ENFORCED_PREFIX = PREFIX.replace('staging read dry run', 'staging read limit')
ENFORCED_SERVER = SERVER.replace('Observe only;', 'Reject excess reads;').replace(
    'limit_req_dry_run on;', 'limit_req_dry_run off;')


def instrument_read_enforcement(text):
    if ENFORCED_PREFIX in text or ENFORCED_SERVER in text:
        if text.count(ENFORCED_PREFIX) != 1 or text.count(ENFORCED_SERVER) != 1:
            raise ValueError('Partial enforced policy; inspect before changing')
        text = text.replace(ENFORCED_PREFIX, PREFIX, 1).replace(ENFORCED_SERVER, SERVER, 1)
    validated = instrument_read_dry_run(text)
    return validated.replace(PREFIX, ENFORCED_PREFIX, 1).replace(SERVER, ENFORCED_SERVER, 1)
