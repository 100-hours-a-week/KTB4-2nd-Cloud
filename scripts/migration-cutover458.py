#!/usr/bin/env python3
"""Narrow overlays for the immutable #455 / four-partition rehearsal bundle.

No workload, offsets, Source fence, GTID checks or fixed deadlines are changed.
Only JDBC shutdown confirmation moves after proof publication. Credentials are
prepared before the fixed fence and reused for promotion, never persisted here.
"""
import json
from pathlib import Path
import time


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('Pinned cutover template differs; refusing an ambiguous overlay')
    return text.replace(before, after, 1)


def cleanup_sinks(run, api, *, seconds=60, clock=time.monotonic, pause=time.sleep):
    """Idempotent, bounded, Run-scoped cleanup; never changes Writer/Source/offsets.

    Called AFTER the signed durable-tail proof is delivered. Failure is an
    outstanding cleanup, not permission to reopen Source or revoke that proof.
    Each HTTP request shares the remaining cleanup budget (at most two seconds).
    """
    from uuid import UUID
    if str(UUID(run)) != run:
        raise ValueError('Canonical Run required')
    end = clock() + seconds
    names = ['yeodam-b391-' + run[:8] + '-' + suffix for suffix in ('sink', 'sink-journal')]
    states = {name: {'stopped': False} for name in names}
    requested = set()
    while clock() < end:
        for name in names:
            if states[name]['stopped'] or clock() >= end:
                continue
            try:
                if name not in requested:
                    api('/connectors/' + name + '/stop', 'PUT', timeout=min(2, max(.01, end-clock())))
                    requested.add(name)
                status = api('/connectors/' + name + '/status', timeout=min(2, max(.01, end-clock())))
                states[name] = {'stopped': status['connector']['state'] == 'STOPPED' and not status['tasks'],
                                'connector': status['connector']['state'], 'tasks': [t['state'] for t in status['tasks']]}
            except Exception as error:
                states[name]['errorClass'] = type(error).__name__
                # A timed-out stop may have been accepted. Repeating stop is safe.
                requested.discard(name)
        if all(value['stopped'] for value in states.values()):
            break
        pause(min(.2, max(0, end-clock())))
    return {'runId': run, 'status': 'completed' if all(v['stopped'] for v in states.values()) else 'pending',
            'connectors': states, 'finishedAt': time.time(), 'budgetSeconds': seconds,
            'writerAction': 'none', 'sourceAction': 'none', 'offsetAction': 'none'}


def record_cleanup(run, api):
    directory = Path('/var/lib/yeodam/b458')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = cleanup_sinks(run, api)
    path = directory / (run + '.sink-cleanup.json')
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value)); temporary.chmod(0o600); temporary.replace(path)
    print(json.dumps({'runId': run, 'sinkCleanup': value['status']}), flush=True)
    return value


def render_boundary(text):
    text = replace_once(text, 'def api(path,method=None):', 'def api(path,method=None,*,timeout=10):')
    text = replace_once(text, 'method=method),timeout=10)', 'method=method),timeout=timeout)')
    start = text.index("        mark('sink_stop')")
    end = text.index("        if time.time()>deadline:", start)
    expected = """        mark('sink_stop')
        for kind in ('sink','sink-journal'):
            api('/connectors/'+n+'-'+kind+'/stop','PUT')
            while time.time()<deadline:
                status=api('/connectors/'+n+'-'+kind+'/status')
                if status['connector']['state']=='STOPPED' and not status['tasks']:break
                time.sleep(.05)
            else:raise BoundaryError('SINK_STOP_DEADLINE',state)
"""
    if text[start:end] != expected:
        raise ValueError('Pinned Sink shutdown path differs')
    text = text[:start] + "        mark('sink_cleanup_deferred')\n" + text[end:]
    text = replace_once(text, "'boundaryMethod':'frozen-business-gtids-final-marker-and-graceful-flush'",
                        "'boundaryMethod':'frozen-source-gtids-and-jdbc-durable-committed-tail',"
                        "'sinkCleanup':'deferred-after-proof'")
    text = replace_once(text, '        if on_proof:on_proof(proof)\n        return proof', """        if on_proof:on_proof(proof)
        # DB commit -> markProcessed -> durable broker offset was verified for
        # Debezium JDBC 3.4.3.Final. Sources are STOPPED and tails immutable.
        # A new Target writer cannot be overwritten by remaining historical work:
        # every original partition is durably committed at its final end already.
        # Never reset/replay this group or resume the Source after promotion.
        if on_proof:
            try:
                from importlib.machinery import SourceFileLoader
                helper=SourceFileLoader('cleanup458','/run/yeodam-b391/migration-cutover458.py').load_module()
                helper.record_cleanup(run,api)
            except Exception as error:
                # Proof delivery already succeeded. Cleanup cannot revoke it.
                print(json.dumps({'runId':run,'sinkCleanup':'pending','errorClass':type(error).__name__}),flush=True)
        return proof""")
    return text


def render_control(text):
    text = replace_once(text, 'def main():', 'def main(argv=None,shared_db=None):')
    text = replace_once(text, 'a=p.parse_args()', 'a=p.parse_args(argv)')
    text = replace_once(text, '    db=database()', '    db=database() if shared_db is None else shared_db')
    return replace_once(text, '    finally:db.close()', '    finally:\n        if shared_db is None:db.close()')


def render_handoff(text):
    text = replace_once(text, "    def action(name,*extra):\n", """    # Materialize private parameters before the scheduled fence. Every action
    # still performs fresh role/GTID/Binlog checks against the live databases.
    control=SourceFileLoader('control458',str(ROOT/'b-integration376-control.py')).load_module()
    db=control.database()
    result['credentialsPreparedAt']=time.time()
    def action(name,*extra):
""")
    text = replace_once(text, """        p=subprocess.run(['python3',str(ROOT/'b-integration376-control.py'),name,'--run-id',run,*extra],capture_output=True,text=True)
        if p.returncode:raise RuntimeError('Local handoff action failed: '+name)""", """        started=time.time()
        try:control.main([name,'--run-id',run,*extra],shared_db=db)
        finally:result.setdefault('actionTimings',[]).append({'action':name,'startedAt':started,'finishedAt':time.time()})""")
    return replace_once(text, "        c.close();result['finishedAt']", "        db.close();c.close();result['finishedAt']")
