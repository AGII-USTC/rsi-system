"""Portable content identities for an existing, read-only pi0.5 installation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def content_id(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _files(root, group):
    root = Path(root).resolve(strict=True)
    if group == 'checkpoint':
        folders = [root / 'params', root / 'assets']
        if any(not p.is_dir() for p in folders):
            raise ValueError('Checkpoint needs existing params and assets directories')
        paths = [p for folder in folders for p in folder.rglob('*') if p.is_file()]
        if any(not any(p.is_relative_to(folder) for p in paths) for folder in folders):
            raise ValueError('Checkpoint params and assets must contain files')
    elif group == 'openpi':
        folders = [root / 'src', root / 'packages/openpi-client/src']
        paths = [p for folder in folders for p in folder.rglob('*.py') if p.is_file()]
        if not (root / 'src/openpi/models/pi0.py').is_file():
            raise ValueError('OpenPI root lacks src/openpi/models/pi0.py')
    elif group == 'memory':
        paths = [p for p in root.rglob('*.py') if p.is_file()]
        if not (root / 'latent_policy.py').is_file():
            raise ValueError('Memory root lacks latent_policy.py')
    else:
        raise ValueError('Unknown binding group')
    if not paths:
        raise ValueError('Empty binding group: ' + group)
    return sorted(p.relative_to(root).as_posix() for p in paths)


def _inside(root, relative):
    root = Path(root).resolve(strict=True)
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Binding paths must be relative')
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Binding leaves its read-only root')
    return path


def build_manifest(roots, artifacts):
    groups = {}
    for group in ('checkpoint', 'openpi', 'memory'):
        groups[group] = [
            {'path': rel, 'bytes': _inside(roots[group], rel).stat().st_size,
             'sha256': sha256_file(_inside(roots[group], rel))}
            for rel in _files(roots[group], group)
        ]
    bound = {}
    for name, path in artifacts.items():
        rel = Path(path).resolve(strict=True).relative_to(Path(roots['artifacts']).resolve(strict=True)).as_posix()
        file = _inside(roots['artifacts'], rel)
        bound[name] = {'path': rel, 'bytes': file.stat().st_size, 'sha256': sha256_file(file)}
    if set(bound) != {'adapter', 'report', 'request'}:
        raise ValueError('Bind exactly adapter, report and request')
    body = {'schema_version': 1, 'base_config': 'pi05_libero', 'groups': groups, 'artifacts': bound}
    return {**body, 'content_id': content_id(body)}


def verify_manifest(manifest, roots):
    body = {k: v for k, v in manifest.items() if k != 'content_id'}
    if manifest.get('schema_version') != 1 or manifest.get('base_config') != 'pi05_libero':
        raise ValueError('Unsupported pi0.5 binding manifest')
    if content_id(body) != manifest.get('content_id'):
        raise ValueError('Binding manifest content_id mismatch')
    if set(manifest['groups']) != {'checkpoint', 'openpi', 'memory'}:
        raise ValueError('Incomplete binding groups')
    for group, entries in manifest['groups'].items():
        if [item['path'] for item in entries] != _files(roots[group], group):
            raise ValueError('File inventory changed: ' + group)
        for item in entries:
            file = _inside(roots[group], item['path'])
            if file.stat().st_size != item['bytes'] or sha256_file(file) != item['sha256']:
                raise ValueError('Content changed: ' + group + '/' + item['path'])
    if set(manifest['artifacts']) != {'adapter', 'report', 'request'}:
        raise ValueError('Incomplete bound artifacts')
    resolved = {}
    for name, item in manifest['artifacts'].items():
        file = _inside(roots['artifacts'], item['path'])
        if file.stat().st_size != item['bytes'] or sha256_file(file) != item['sha256']:
            raise ValueError('Content changed: artifact ' + name)
        resolved[name] = file
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('build', 'verify'))
    parser.add_argument('--manifest', type=Path, required=True)
    for name in ('checkpoint', 'openpi', 'memory', 'artifacts'):
        parser.add_argument('--' + name + '-root', type=Path, required=True)
    for name in ('adapter', 'report', 'request'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    roots = {name: getattr(args, name + '_root') for name in ('checkpoint', 'openpi', 'memory', 'artifacts')}
    if args.operation == 'build':
        if any(getattr(args, name) is None for name in ('adapter', 'report', 'request')):
            parser.error('build needs --adapter, --report and --request')
        if args.manifest.exists():
            parser.error('Refusing to overwrite an existing binding manifest')
        manifest = build_manifest(roots, {name: getattr(args, name) for name in ('adapter', 'report', 'request')})
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open('x') as output:
            json.dump(manifest, output, indent=2)
            output.write('\n')
    else:
        manifest = json.loads(args.manifest.read_text())
        verify_manifest(manifest, roots)
    print(json.dumps({'status': 'verified' if args.operation == 'verify' else 'built',
                      'content_id': manifest['content_id']}))


if __name__ == '__main__':
    main()
