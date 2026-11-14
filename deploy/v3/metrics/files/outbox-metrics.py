"""Fresh bounded aggregates only. Raw receipts and current business work are distinct."""
from pathlib import Path
import math
import time
TABLES = ('trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox','attachment_conversion_outbox')
CLASSES = ('active','succeeded','canceled','failed','unknown')
KEYS = set(TABLES) | {'conversion_'+c for c in CLASSES}

def metrics(path='/outbox/snapshot', now=None):
    now = time.time() if now is None else now
    try:
        data = Path(path).read_text()
        if len(data) > 2048: raise ValueError()
        lines = data.splitlines()
        # Legacy integer snapshots remain readable during rolling replacement.
        timing = lines[0].split('\t')
        started, stamp = (float(timing[0]), float(timing[-1]))
        if len(timing) not in (1, 2) or not all(math.isfinite(x) for x in (started, stamp)) or not 0 <= stamp-started <= 8.5: raise ValueError()
        if not 0 <= now-stamp <= 65 or len(lines) != 1+len(KEYS): raise ValueError()
        samples = {}
        for line in lines[1:]:
            key, count, age = line.split('\t')
            count, age = int(count), float(age)
            if key not in KEYS or key in samples or not 0 <= count <= 10001 or not math.isfinite(age) or age < 0 or (count == 0 and age != 0): raise ValueError()
            samples[key] = (count, age)
        if set(samples) != KEYS or sum(samples['conversion_'+c][0] for c in CLASSES) != samples['attachment_conversion_outbox'][0]: raise ValueError()
        result = ['yeodam_outbox_probe_success 1', f'yeodam_outbox_last_success_timestamp_seconds {stamp}']
        if len(timing) == 2:
            result += [f'yeodam_outbox_query_started_timestamp_seconds {started}',
                       f'yeodam_outbox_query_completed_timestamp_seconds {stamp}',
                       f'yeodam_outbox_query_duration_seconds {stamp-started:.6f}']
        for table in TABLES:
            count, age = samples[table]
            labels = '{outbox="'+table+'"}'
            result += [f'yeodam_outbox_pending{labels} {count}', f'yeodam_outbox_count_capped{labels} {int(count == 10001)}', f'yeodam_outbox_oldest_pending_age_seconds{labels} {age}']
        result += ['yeodam_conversion_outbox_classification_capped '+str(int(samples['attachment_conversion_outbox'][0] == 10001))]
        for category in CLASSES:
            count, age = samples['conversion_'+category]
            labels = '{category="'+category+'"}'
            result += [f'yeodam_conversion_outbox_pending{labels} {count}', f'yeodam_conversion_outbox_oldest_pending_age_seconds{labels} {age}']
        return '\n'.join(result)+'\n'
    except (OSError, ValueError, IndexError):
        return 'yeodam_outbox_probe_success 0\n'
