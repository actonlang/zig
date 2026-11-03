#!/usr/bin/env python3
"""Publish all four tested archives together; never replace an existing release."""
import json
import os
from pathlib import Path
import subprocess
import sys
from common import digest

root = Path(__file__).resolve().parents[2]
version = (root / 'ci/acton/version').read_text().strip()
packages = Path(sys.argv[1]).resolve()
platforms = ['x86_64-linux', 'aarch64-linux', 'x86_64-macos', 'aarch64-macos']
archives = [packages / f'zig-{target}-{version}.tar.xz' for target in platforms]
if set(packages.glob('*.tar.xz')) != set(archives):
    raise SystemExit('Release must contain exactly the four expected platform archives')
lines = []
for archive in archives:
    expected = archive.with_suffix('.xz.sha256').read_text().strip()
    actual = digest(archive) + '  ' + archive.name
    if actual != expected:
        raise SystemExit('Archive checksum mismatch: ' + archive.name)
    lines.append(actual)
checksums = packages / 'SHA256SUMS'
checksums.write_text('\n'.join(lines) + '\n')
revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
if revision != os.environ['GITHUB_SHA']:
    raise SystemExit('Checkout differs from the tested workflow revision')
identity = packages / 'release.json'
identity.write_text(json.dumps({'version': version, 'source_commit': revision,
                               'archives': {p.name: digest(p) for p in archives}}, indent=2) + '\n')
tag = 'acton-' + version
repo = 'actonlang/zig'
tag_lookup = subprocess.run(['git', 'ls-remote', '--exit-code', '--tags',
                             'https://github.com/' + repo + '.git', 'refs/tags/' + tag],
                            capture_output=True, text=True)
if tag_lookup.returncode == 0:
    raise SystemExit('Release tag already exists; use a new version')
if tag_lookup.returncode != 2:
    raise SystemExit('Could not verify release tag availability: ' + tag_lookup.stderr)
existing = subprocess.run(['gh', 'release', 'view', tag, '--repo', repo], capture_output=True)
if existing.returncode == 0:
    raise SystemExit('Release already exists; use a new version rather than replace it')
notes = packages / 'release-notes.txt'
notes.write_text('Zig builds for Acton, based on upstream 097734a31d. Each archive includes the compiler and its matching library sources.\n')
subprocess.run(['gh', 'release', 'create', tag, '--repo', repo, '--target', revision,
                '--title', version, '--notes-file', str(notes), '--draft',
                *map(str, archives), str(checksums), str(identity)], check=True)
subprocess.run(['gh', 'release', 'edit', tag, '--repo', repo, '--draft=false', '--prerelease'], check=True)
