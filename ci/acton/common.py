"""Verified release inputs shared by the compiler and LLVM builders."""
import hashlib
from pathlib import Path
import tarfile
import urllib.request


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for data in iter(lambda: source.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def fetch(spec, downloads):
    downloads.mkdir(parents=True, exist_ok=True)
    path = downloads / spec['url'].rsplit('/', 1)[1]
    if path.exists():
        if path.stat().st_size != spec['size'] or digest(path) != spec['sha256']:
            raise SystemExit(f'Cached input size or SHA256 mismatch: {path}')
        return path
    partial = path.with_name(path.name + '.part')
    try:
        with urllib.request.urlopen(spec['url'], timeout=120) as source, partial.open('wb') as out:
            while data := source.read(1024 * 1024):
                out.write(data)
        if partial.stat().st_size != spec['size'] or digest(partial) != spec['sha256']:
            raise SystemExit(f'Download size or SHA256 mismatch: {path}')
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def unpack(archive, destination, expected_sha):
    marker = destination / '.archive-sha256'
    if destination.exists():
        if not marker.exists() or marker.read_text().strip() != expected_sha:
            raise SystemExit(f'Existing directory has different inputs: {destination}')
    else:
        destination.mkdir(parents=True)
        with tarfile.open(archive) as source:
            source.extractall(destination, filter='data')
        marker.write_text(expected_sha + '\n')
    roots = [p for p in destination.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise SystemExit(f'Expected one archive root in {archive}')
    return roots[0]
