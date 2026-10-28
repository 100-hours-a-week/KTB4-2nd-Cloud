"""Read-only local browser guard for #447's existing bounded H2 load controller."""
from datetime import datetime, timezone
import json
from pathlib import Path


def healthy(directory, capture_id, at=None):
    try:
        directory = Path(directory)
        if (directory/'GUARD').exists():
            return False
        row = json.loads((directory/'checkpoint.json').read_text())
        age = ((at or datetime.now(timezone.utc))-datetime.fromisoformat(row['updatedAt'])).total_seconds()
        return (row['version'] == 1 and row['captureId'] == capture_id
                and row['origin'] == 'https://v1-staging.yeodam-2gether.com'
                and -5 <= age <= 60 and row['ready'] is True and row['failed'] is False
                and row['result']['ok'] is True
                and set(row['completedViews']) == {'login', 'home', 'list', 'detail'})
    except (OSError, ValueError, KeyError, TypeError):
        return False
