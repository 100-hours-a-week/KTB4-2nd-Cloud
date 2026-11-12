"""Render only complete, fresh, bounded aggregate samples; never expose rows/SQL/errors."""
from pathlib import Path
import math
import time
TABLES = ('trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox','attachment_conversion_outbox')
def metrics(path='/outbox/snapshot', now=None):
    now = time.time() if now is None else now
    try:
        data = Path(path).read_text()
        if len(data) > 2048: raise ValueError()
        lines = data.splitlines()
        stamp = int(lines[0])
        if not 0 <= now-stamp <= 65 or len(lines) != 5: raise ValueError()
        result = ['yeodam_outbox_probe_success 1', f'yeodam_outbox_last_success_timestamp_seconds {stamp}']
        seen = set()
        for line in lines[1:]:
            table, count, age = line.split('\t')
            count, age = int(count), float(age)
            if table not in TABLES or table in seen or not 0 <= count <= 10001 or not math.isfinite(age) or age < 0 or (count == 0 and age != 0): raise ValueError()
            seen.add(table)
            labels = '{outbox="'+table+'"}'
            result += [f'yeodam_outbox_pending{labels} {count}', f'yeodam_outbox_count_capped{labels} {int(count == 10001)}', f'yeodam_outbox_oldest_pending_age_seconds{labels} {age}']
        if seen != set(TABLES): raise ValueError()
        return '\n'.join(result)+'\n'
    except (OSError, ValueError, IndexError):
        return 'yeodam_outbox_probe_success 0\n'
