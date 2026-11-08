#!/usr/bin/env python3
"""Assemble public, already-reviewed rollout inputs without reading AWS or Kubernetes."""
import argparse
import json
import os
import yaml
from pathlib import Path
import v3_release_bundle as bundle
from prepare_v3_candidate_helpers import local_charts
from v3_service_access import review, values


def assemble(components, profiles, services):
    evidence = review(services)
    return bundle.normalize({'schemaVersion': 2, 'target': 'yeodam-v3-foundation', 'state': 'prepared',
                             'components': components, 'values': profiles | {'service-access': values(evidence)}, 'serviceReview': evidence})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--components', required=True, type=Path, help='Four publisher Runs, source commits and immutable image digests')
    parser.add_argument('--services', required=True, type=Path, help='Public List containing ai/ai-api and trip/trip-api only')
    for name in ['frontend', 'backend', 'websocket', 'ai']:
        parser.add_argument('--'+name, required=True, type=Path, help='Merged public Helm values; no credentials')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    bundle.require(not args.output.exists(), 'Refusing to overwrite an existing candidate')
    profiles = {n: yaml.load(getattr(args, n).read_text(), Loader=bundle.apps.platform.UniqueLoader)
                for n in ['frontend', 'backend', 'websocket', 'ai']}
    data = assemble(bundle.parse_json(args.components.read_text()), profiles, bundle.parse_json(args.services.read_text()))
    bundle.validate(bundle.build(data, local_charts(), {'candidatePr': 0, 'cloudCommit': '0'*40, 'cloudGateRunId': 0}))
    os.umask(0o077)
    args.output.write_text(json.dumps(data, indent=2)+'\n')
    print('Prepared public candidate only; publisher verification and candidate-only PR required before approval')


if __name__ == '__main__':
    main()
