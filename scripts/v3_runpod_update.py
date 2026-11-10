"""Receipt-backed V3-only model URL update. No ECS/SSM shell/GPU lifecycle calls."""
import copy
import hashlib
import json
import re
import uuid

SECRET = 'yeodam/v3/foundation/ai/runtime'
WORKLOADS = ('ai-runner', 'ai-api')
MODEL = 'gemma-4'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_url(value):
    require(isinstance(value, str) and re.fullmatch(r'https://[a-z0-9]+-8000\.proxy\.runpod\.net/v1', value),
            'Use exactly https://<pod-id>-8000.proxy.runpod.net/v1')
    return value


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def perform(url, secrets, cluster, probe, save):
    """Adapters make concurrency, partial-failure and lost-response paths testable."""
    validate_url(url)
    version, original = secrets.current()
    validate_url(original.get('VLM_BASE_URL'))
    require(bool(original.get('VLM_API_KEY')), 'Existing V3 VLM key required')
    operation = uuid.uuid4().hex
    report = {'schemaVersion': 1, 'operation': operation, 'secret': SECRET,
              'previousVersion': version, 'candidateVersion': operation,
              'previousUrlSha256': sha(original['VLM_BASE_URL']), 'newUrlSha256': sha(url),
              'phase': 'preflight', 'completed': False, 'recovered': False}
    save(report)
    cluster.preflight(sha(original['VLM_BASE_URL']))
    probe(url, original['VLM_API_KEY'], MODEL)
    if original['VLM_BASE_URL'] == url:
        report.update(phase='unchanged', completed=True)
        save(report)
        return report
    changed = False
    attempted_promotion = False
    try:
        cluster.drain(operation)
        report['phase'] = 'drained'
        save(report)
        # The loop uses its stop Event, including photo batches; idle is not just
        # one racy queue-size observation. No active record is reset or reissued.
        cluster.assert_idle(operation)
        require(secrets.current()[0] == version, 'Secret changed during drain; do not overwrite it')
        desired = copy.deepcopy(original)
        desired['VLM_BASE_URL'] = url
        secrets.stage(operation, desired)
        report['phase'] = 'promotion_pending'
        save(report)
        attempted_promotion = True
        secrets.move(operation, version)
        changed = True
        report['phase'] = 'promoted'
        save(report)
        cluster.sync(sha(url))
        for name in WORKLOADS:
            require(secrets.current()[0] == operation, 'Secret ownership changed during replacement')
            cluster.replace(name, operation, sha(url))
        cluster.complete()
        report.update(phase='complete', completed=True)
        save(report)
        return report
    except BaseException:
        if not attempted_promotion:
            report['phase'] = 'failed_before_promotion'
            save(report)
        # A lost UpdateSecretVersionStage response is not proof it did not move.
        if attempted_promotion:
            try:
                current = secrets.current()[0]
                changed = current == operation
                if current not in {version, operation}:
                    report['phase'] = 'conflict_manual_recovery'
                    save(report)
                    raise RuntimeError('Another version owns AWSCURRENT; automatic recovery refused') from None
                if changed:
                    cluster.drain(operation)
                    cluster.assert_idle(operation)
                    report['phase'] = 'recovery_pending'
                    save(report)
                    secrets.move(version, operation)
                    cluster.sync(sha(original['VLM_BASE_URL']))
                    for name in WORKLOADS:
                        require(secrets.current()[0] == version, 'Secret changed during recovery')
                        cluster.replace(name, operation, sha(original['VLM_BASE_URL']))
                    report.update(phase='recovered', recovered=True)
                    save(report)
            except BaseException:
                if report['phase'] != 'conflict_manual_recovery':
                    report['phase'] = 'recovery_incomplete'
                    save(report)
                raise RuntimeError('Update failed; inspect the public receipt before recovery') from None
        raise RuntimeError('Update failed; previous configuration preserved or recovered') from None
    finally:
        # Per-Pod ownership prevents clearing another run's drain. A killed
        # workflow also has a bounded lease: it cannot strand the queue forever.
        cluster.resume(operation)
