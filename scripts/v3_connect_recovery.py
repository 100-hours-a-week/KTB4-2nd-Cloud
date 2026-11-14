"""Allowlisted Connect group/assignment evidence. Raw log text is never returned."""
from datetime import datetime, timezone
import re

CONNECTOR = 'yeodam-v3-outbox'
MAX_BYTES = 2 * 1024 * 1024


def stamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)',value):
        raise ValueError('Explicit timezone timestamp required')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.utcoffset().total_seconds()!=0:result=result.astimezone(timezone.utc)
    return result


def parse_events(raw, start, end):
    lower,upper=stamp(start),stamp(end)
    if not 0 < (upper-lower).total_seconds() <= 1800:
        raise ValueError('Use a nonempty window of at most thirty minutes')
    if len(raw.encode()) > MAX_BYTES:raise ValueError('Connect log response exceeds bound')
    events=[];rejected=0
    for line in raw.splitlines():
        # Only Kafka's fixed Connect group assignment message, never arbitrary app text.
        if ' INFO ' not in line or not re.search(r'(?:DistributedHerder:\d+ - |\(org\.apache\.kafka\.connect\.runtime\.distributed\.DistributedHerder:\d+\)$)',line):continue
        match=re.search(r'\[Worker clientId=[^]\r\n]+, groupId=connect-v3\] Joined group at generation (\d+) with protocol version (\d+) and got assignment: Assignment\{([^}\r\n]+)\}',line)
        if not match:continue
        at=line.split(' ',1)[0];generation,protocol,body=match.groups()
        try:when=stamp(at)
        except ValueError:continue
        if not lower<=when<=upper:continue
        error=re.search(r'\berror=(\d+)(?:,|$)',body)
        delay=re.search(r'\bdelay=(\d+)(?:,|$)',body)
        connectors=re.search(r'\bconnectorIds=\[([^]]*)\]',body)
        tasks=re.search(r'\btaskIds=\[([^]]*)\]',body)
        if not all([error,delay,connectors,tasks]):rejected+=1;continue
        cs=[v.strip()for v in connectors[1].split(',')if v.strip()]
        ts=[v.strip()for v in tasks[1].split(',')if v.strip()]
        if (error[1]!='0' or int(delay[1])>2147483647 or
                cs not in ([],[CONNECTOR]) or ts not in ([],[CONNECTOR+'-0'])):
            rejected+=1;continue
        events.append({'at':at,'event':'group_joined_assignment','generation':int(generation),
                       'protocolVersion':int(protocol),'scheduledDelayMs':int(delay[1]),
                       'connectorAssigned':bool(cs),'task0Assigned':bool(ts)})
    events.sort(key=lambda x:stamp(x['at']))
    # A missing/rotated marker remains unknown, never a fabricated zero-second wait.
    waits=[];empty=None
    for event in events:
        if not event['connectorAssigned'] and not event['task0Assigned']:
            if empty is None:empty=event
        elif event['task0Assigned'] and empty is not None:
            waits.append({'emptyAssignmentAt':empty['at'],'taskAssignedAt':event['at'],
                          'elapsedSeconds':(stamp(event['at'])-stamp(empty['at'])).total_seconds(),
                          'scheduledDelayMs':empty['scheduledDelayMs']});empty=None
    return {'events':events,'assignmentWaits':waits,'openEmptyAssignment':empty,
            'rejectedAssignmentLines':rejected,'rawLogsPersisted':False,
            'coverage':'bounded current-container log; rotation and missing markers possible',
            'businessRecoveryAssessed':False}
