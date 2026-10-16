#!/usr/bin/env python3
"""Read GitHub PR Gate timing metadata and analyze the saved snapshot offline."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


REPOSITORIES = {
    'FE': ('KTB4-2nd-FE', 'frontend-pr-gate.yml'),
    'BE': ('KTB4-2nd-BE', 'backend-pr-gate.yml'),
    'AI': ('KTB4-2nd-AI', 'ai-pr-gate.yml'),
    'Cloud': ('KTB4-2nd-Cloud', 'cloud-pr-gate.yml'),
}
OWNER = '100-hours-a-week'
VERIFICATION = re.compile(r'lint|format$|type.?check|unit tests|^test$|run tests|validate |run isolated|check model load|build.*(?:image|pushing)', re.I)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def api(endpoint):
    # Explicit GET, fixed GitHub host, no shell or access-token arguments.
    try:
        result = subprocess.run(
            ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', endpoint],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'GitHub GET could not complete: {endpoint}') from error
    if result.returncode:
        raise RuntimeError(f'GitHub GET failed ({result.returncode}): {endpoint}')
    return json.loads(result.stdout)


def pages(endpoint, key):
    rows = []
    for page in range(1, 101):
        data = api(f'{endpoint}?per_page=100&page={page}')
        current = data[key]
        rows.extend(current)
        if len(current) < 100:
            return rows
    raise RuntimeError('Pagination safety limit reached; snapshot is incomplete')


def collect_repository(label, destination, limit):
    repo, workflow = REPOSITORIES[label]
    prefix = f'repos/{OWNER}/{repo}'
    root = destination / label
    result = {'repository': label, 'complete': False, 'runs': [], 'errors': []}
    try:
        data = api(f'{prefix}/actions/workflows/{workflow}/runs?event=pull_request&per_page={limit}')
        save(root / 'runs.json', data)
        seen = set()
        for run in data['workflow_runs']:
            run_id, sha = run['id'], run['head_sha']
            if not re.fullmatch(r'[0-9a-f]{40}', sha):
                raise ValueError('Unexpected source SHA')
            if sha not in seen:
                source = api(f'{prefix}/contents/.github/workflows/{workflow}?ref={sha}')
                text = base64.b64decode(source['content']).decode()
                path = root / 'workflows' / f'{sha}.yml'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                # Commit file names describe this commit, NOT the entire PR diff.
                commit = api(f'{prefix}/commits/{sha}?per_page=100')
                names = [f['filename'] for f in commit.get('files', [])]
                save(root / 'commits' / f'{sha}.json', {
                    'sha': sha, 'files': names,
                    'files_complete': len(names) < 100,
                    'scope': 'head-commit-only',
                })
                seen.add(sha)
            for attempt in range(1, run.get('run_attempt', 1) + 1):
                detail = api(f'{prefix}/actions/runs/{run_id}/attempts/{attempt}')
                if detail.get('run_attempt') != attempt:
                    raise ValueError('Attempt metadata does not match the requested attempt')
                jobs = pages(f'{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
                relative = Path(label) / 'attempts' / f'{run_id}-{attempt}.json'
                save(destination / relative, {'run': detail, 'jobs': jobs})
                result['runs'].append(str(relative))
        result['complete'] = True
    except (RuntimeError, ValueError, KeyError) as error:
        result['errors'].append(str(error))
    return result


def seconds(start, end):
    if not start or not end:
        return None
    try:
        value = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
                 datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
    except (ValueError, TypeError):
        return None
    return value if value >= 0 else None


def workflow_graph(text):
    """Only the literal job/name/needs subset in these four workflows is supported.

    Matrix, expressions, YAML aliases and unfamiliar needs syntax return unknown,
    never an invented dependency graph. The source file remains in the snapshot.
    """
    jobs, current, in_jobs, needs_block = {}, None, False, False
    for line in text.splitlines():
        if line == 'jobs:':
            in_jobs = True
            continue
        if not in_jobs or not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line.startswith(' '):
            break
        match = re.fullmatch(r'  ([\w-]+):\s*', line)
        if match:
            current = match[1]
            jobs[current] = {'name': current, 'needs': []}
            needs_block = False
            continue
        if current is None:
            return None
        if re.search(r'^\s*<<:|\s[&*][A-Za-z_]', line):
            return None
        if re.match(r'    (strategy|uses):', line):
            return None
        if re.match(r'    \S', line):
            needs_block = False
        if line.startswith('    name:'):
            name = line.partition(':')[2].strip().strip('"\'')
            if not re.fullmatch(r'[\w .()/-]+', name):
                return None
            jobs[current]['name'] = name
        if line.startswith('    needs:'):
            value = line.partition(':')[2].strip()
            if not value:
                needs_block = True
            else:
                if not re.fullmatch(r'[\w\s,\[\]\'"-]+', value):
                    return None
                jobs[current]['needs'] = [v.strip().strip('"\'') for v in value.strip('[]').split(',')]
        elif needs_block and line.strip():
            match = re.fullmatch(r'      - ([\w-]+)', line)
            if not match:
                return None
            jobs[current]['needs'].append(match[1])
    if not jobs or len({j['name'] for j in jobs.values()}) != len(jobs):
        return None
    graph = {j['name']: [jobs[d]['name'] for d in j['needs']] for j in jobs.values()
             if all(d in jobs for d in j['needs'])}
    if len(graph) != len(jobs):
        return None
    def visit(name, ancestors):
        return name not in ancestors and all(visit(d, ancestors | {name}) for d in graph[name])
    return graph if all(visit(name, set()) for name in graph) else None


def change_kind(commit):
    if not commit.get('files_complete') or not commit.get('files'):
        return 'unknown'
    files = commit['files']
    if all(Path(f).suffix.lower() in ('.md', '.pdf') for f in files):
        return 'documentation-only'
    if any(Path(f).name in ('package-lock.json', 'uv.lock', 'build.gradle', 'pyproject.toml') for f in files):
        return 'dependencies'
    if any('Dockerfile' in f or f.startswith(('.github/', 'docker/', 'deploy/')) for f in files):
        return 'runtime-or-ci'
    if all(f.startswith(('tests/', 'src/test/')) or '.test.' in f or '.spec.' in f for f in files):
        return 'tests-only'
    return 'application-or-other'


def analyze_attempt(label, payload, workflow, commit):
    run, jobs = payload['run'], payload['jobs']
    attempt = run.get('run_attempt', 1)
    origin = run.get('created_at') if attempt == 1 else run.get('run_started_at')
    graph = workflow_graph(workflow)
    by_name = {j['name']: j for j in jobs}
    if len(by_name) != len(jobs) or graph is None or set(graph) != set(by_name):
        graph = None
    active = [j for j in jobs if j.get('conclusion') != 'skipped']
    finished = [j['completed_at'] for j in active if j.get('completed_at')]
    complete = (run.get('status') == 'completed' and bool(active) and
                all(j.get('status') == 'completed' and seconds(j.get('started_at'), j.get('completed_at')) is not None for j in active))
    steps, job_rows, failures, feedback, verified_steps = [], [], [], [], []
    for job in jobs:
        skipped = job.get('conclusion') == 'skipped'
        duration = None if skipped else seconds(job.get('started_at'), job.get('completed_at'))
        ready = None
        if graph is not None and not skipped:
            dependencies = [by_name[n] for n in graph[job['name']]]
            if all(d.get('status') == 'completed' and d.get('completed_at') for d in dependencies):
                ready = max([origin] + [d['completed_at'] for d in dependencies]) if origin else None
        row = {'repository': label, 'run_id': run['id'], 'attempt': attempt,
               'job': job['name'], 'conclusion': job.get('conclusion'),
               'seconds': duration, 'ready_to_start_seconds': seconds(ready, job.get('started_at')),
               'start_offset_seconds': None if skipped else seconds(origin, job.get('started_at')),
               'runner_labels': job.get('labels', [])}
        job_rows.append(row)
        for step in job.get('steps', []):
            skipped_step = step.get('conclusion') == 'skipped'
            steps.append({**{k: row[k] for k in ('repository', 'run_id', 'attempt', 'job')},
                          'step': step['name'], 'conclusion': step.get('conclusion'),
                          'seconds': None if skipped_step else seconds(step.get('started_at'), step.get('completed_at'))})
            if not skipped_step and step.get('completed_at'):
                if step.get('conclusion') == 'failure':
                    failures.append(step['completed_at'])
                if (step.get('conclusion') in ('success', 'failure') and
                        not step['name'].startswith('Post ') and VERIFICATION.search(step['name'])):
                    feedback.append(step['completed_at'])
                    verified_steps.append(f'{job["name"]}: {step["name"]}')
    last = max(finished) if finished else None
    complete = complete and seconds(origin, last) is not None
    chain = []
    if graph is not None and complete:
        current = max(active, key=lambda j: j['completed_at'])['name']
        while current:
            chain.insert(0, current)
            dependencies = [by_name[n] for n in graph[current] if by_name[n].get('completed_at')]
            current = max(dependencies, key=lambda j: j['completed_at'])['name'] if dependencies else None
    summary = {
        'repository': label, 'run_id': run['id'], 'attempt': attempt,
        'conclusion': run.get('conclusion'), 'status': run.get('status'),
        'created_at': run.get('created_at'), 'head_sha': run.get('head_sha'),
        'checkout_sha': None, 'workflow_sha256': hashlib.sha256(workflow.encode()).hexdigest(),
        'head_commit_change_kind': change_kind(commit), 'pr_change_kind': 'unknown', 'cache_state': 'unknown',
        'timing_complete': complete, 'dependency_graph_known': graph is not None,
        'elapsed_seconds': seconds(origin, last) if complete else None,
        'origin': 'run-created' if attempt == 1 else 'attempt-start',
        'runner_seconds': sum(j['seconds'] for j in job_rows if j['seconds'] is not None),
        'first_failure_seconds': seconds(origin, min(failures)) if failures else None,
        'first_verification_step_seconds': seconds(origin, min(feedback)) if feedback else None,
        'executed_jobs': sorted(j['name'] for j in active),
        'executed_verification_steps': sorted(verified_steps),
        'skipped_jobs': sorted(j['name'] for j in jobs if j.get('conclusion') == 'skipped'),
        'observed_completion_chain': chain, 'url': run.get('html_url'),
    }
    return summary, job_rows, steps


def write_csv(path, rows):
    if not rows:
        path.write_text('')
        return
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                          for k, v in row.items()} for row in rows)


def analyze(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    runs, jobs, steps = [], [], []
    for repository in manifest['repositories']:
        label = repository['repository']
        for relative in repository['runs']:
            payload = json.loads((root / relative).read_text())
            sha = payload['run']['head_sha']
            workflow = (root / label / 'workflows' / f'{sha}.yml').read_text()
            commit = json.loads((root / label / 'commits' / f'{sha}.json').read_text())
            summary, job_rows, step_rows = analyze_attempt(label, payload, workflow, commit)
            runs.append(summary); jobs.extend(job_rows); steps.extend(step_rows)
    save(root / 'summary.json', runs)
    save(root / 'analysis-metadata.json', {
        'analyzed_at': datetime.now(timezone.utc).isoformat(),
        'analyzer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'attempt_count': len(runs),
        'collector_sha256': manifest.get('collector_sha256'),
    })
    for name, rows in [('runs', runs), ('jobs', jobs), ('steps', steps)]:
        write_csv(root / f'{name}.csv', rows)
    groups = {}
    for run in runs:
        # Only comparable first-attempt success metadata; unknown cache stays explicit.
        if run['attempt'] != 1 or run['conclusion'] != 'success' or not run['timing_complete']:
            continue
        key = (run['repository'], run['workflow_sha256'], tuple(run['executed_jobs']),
               tuple(run['executed_verification_steps']), run['head_commit_change_kind'], run['cache_state'])
        groups.setdefault(key, []).append(run['elapsed_seconds'])
    aggregates = []
    for key, values in groups.items():
        aggregates.append({'repository': key[0], 'workflow_sha256': key[1], 'executed_jobs': list(key[2]),
                           'executed_verification_steps': list(key[3]),
                           'head_commit_change_kind': key[4], 'cache_state': key[5], 'count': len(values),
                           'median_seconds': statistics.median(values), 'min_seconds': min(values),
                           'max_seconds': max(values),
                           'p90_seconds': sorted(values)[math.ceil(len(values) * .9) - 1] if len(values) >= 20 else None})
    save(root / 'groups.json', aggregates)
    report = ['# CI timing snapshot', '',
              'Observational history, not a controlled before/after benchmark. Cache and PR-wide changes are unknown.', '',
              '| Repository | Attempts | Success | Failure | Cancelled | Incomplete timing |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for label in REPOSITORIES:
        subset = [r for r in runs if r['repository'] == label]
        if subset:
            report.append(f'| {label} | {len(subset)} | {sum(r["conclusion"] == "success" for r in subset)} | '
                          f'{sum(r["conclusion"] == "failure" for r in subset)} | '
                          f'{sum(r["conclusion"] == "cancelled" for r in subset)} | '
                          f'{sum(not r["timing_complete"] for r in subset)} |')
    report += ['', 'See runs.csv, jobs.csv, steps.csv and groups.json. Rerun attempts and non-success results remain in raw records.',
               'Job labels identify requested runner labels, not actual hardware specifications.',
               'First verification timing uses step-name matching; audit the step list before interpreting feedback.',
               'Start delay follows the literal saved workflow graph when supported; it does not identify the scheduling cause.',
               'Runner seconds are observed execution intervals, not a billing estimate.', '']
    errors = [error for repo in manifest['repositories'] for error in repo['errors']]
    if errors:
        report += ['## Collection incomplete', *errors, '']
    (root / 'report.md').write_text('\n'.join(report))
    return len(runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['collect', 'analyze'])
    parser.add_argument('--output', required=True, type=Path, help='New local snapshot directory; analyze reuses it')
    parser.add_argument('--repository', action='append', choices=list(REPOSITORIES))
    parser.add_argument('--limit', type=int, default=20, help='Latest PR runs per repository (1-100), all available attempts')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    root = args.output.resolve()
    if args.mode == 'collect':
        collector_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        started_at = datetime.now(timezone.utc).isoformat()
        if root.exists():
            parser.error('collect requires a new directory to preserve previous evidence')
        # Evidence must not accidentally enter the Git index.
        checkout = Path(__file__).resolve().parents[1]
        if root.is_relative_to(checkout) and not root.is_relative_to(checkout / '.local-docs'):
            parser.error('inside the checkout, output must be under .local-docs')
        root.mkdir(parents=True, mode=0o700)
        labels = list(dict.fromkeys(args.repository or REPOSITORIES))
        with ThreadPoolExecutor(max_workers=4) as pool:
            repositories = list(pool.map(lambda label: collect_repository(label, root, args.limit), labels))
        save(root / 'manifest.json', {'collected_at': datetime.now(timezone.utc).isoformat(),
                                      'started_at': started_at, 'collector_sha256': collector_sha256,
                                      'limit': args.limit, 'repositories': repositories})
    count = analyze(root)
    print(f'Analyzed {count} attempts; local report: {root / "report.md"}')
    complete = all(r['complete'] for r in json.loads((root / 'manifest.json').read_text())['repositories'])
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
