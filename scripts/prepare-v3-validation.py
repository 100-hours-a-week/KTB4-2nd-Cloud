#!/usr/bin/env python3
"""Install checksum-pinned local render tools, never access a cluster."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def download(entry):
    request = urllib.request.Request(entry['url'], headers={'User-Agent': 'yeodam-v3-validation'})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('Downloaded validation input failed checksum verification')
    return data


def download_karpenter(entry):
    base = 'https://' + entry['registry']
    scope = 'repository:' + entry['repository'] + ':pull'
    token_url = base + '/token/?service=' + entry['registry'] + '&scope=' + scope
    with urllib.request.urlopen(token_url, timeout=120) as response:
        token = json.load(response)['token']
    def fetch(path):
        request = urllib.request.Request(base + path, headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.oci.image.manifest.v1+json'})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    manifest = fetch('/v2/' + entry['repository'] + '/manifests/' + entry['manifestDigest'])
    if 'sha256:' + hashlib.sha256(manifest).hexdigest() != entry['manifestDigest']:
        raise ValueError('Karpenter OCI manifest checksum mismatch')
    layers = json.loads(manifest)['layers']
    if len(layers) != 1 or layers[0]['digest'] != 'sha256:' + entry['sha256']:
        raise ValueError('Unexpected Karpenter chart layer')
    chart = fetch('/v2/' + entry['repository'] + '/blobs/' + layers[0]['digest'])
    if hashlib.sha256(chart).hexdigest() != entry['sha256']:
        raise ValueError('Karpenter chart checksum mismatch')
    return chart


def prepare(directory):
    lock = json.loads((ROOT / 'tests/ci/v3/tools.lock.json').read_text())
    architecture = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    target = f'{platform.system().lower()}-{architecture}'
    if target not in lock['platforms']:
        raise ValueError(f'No reviewed validation binaries for {target}')
    directory.mkdir(parents=True, exist_ok=True)
    for tool, entry in lock['platforms'][target].items():
        # Keep the verified archive; do not trust an unrelated binary on PATH.
        archive = directory / f'{tool}-{entry["sha256"]}.tar.gz'
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != entry['sha256']:
            archive.write_bytes(download(entry))
        with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode='r:gz') as bundle:
            member = bundle.getmember(entry['member'])
            if not member.isfile():
                raise ValueError('Expected a regular validation binary')
            with bundle.extractfile(member) as stream:
                (directory / tool).write_bytes(stream.read())
        (directory / tool).chmod(0o755)
    schema_directory = directory / 'schemas'
    schema_directory.mkdir(exist_ok=True)
    for kind, entry in lock['schemas'].items():
        file = schema_directory / f'{kind}.json'
        if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            file.write_bytes(download(entry))
    # Render this verified archive directly; never unpack arbitrary upstream paths.
    operator = json.loads((ROOT / 'deploy/v3/secret-operator/upstream.lock.json').read_text())
    archive = directory / 'external-secrets.tgz'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != operator['sha256']:
        archive.write_bytes(download(operator))

    strimzi = json.loads((ROOT / 'deploy/v3/kafka-operator/upstream.lock.json').read_text())
    archive = directory / 'strimzi.tgz'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != strimzi['sha256']:
        archive.write_bytes(download(strimzi))

    karpenter = json.loads((ROOT / 'deploy/v3/karpenter/upstream.lock.json').read_text())
    archive = directory / 'karpenter.tgz'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != karpenter['sha256']:
        archive.write_bytes(download_karpenter(karpenter))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    prepare(parser.parse_args().directory)
