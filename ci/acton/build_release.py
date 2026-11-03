#!/usr/bin/env python3
"""Build a relocatable Zig distribution with static LLVM on x86_64 Linux."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from common import digest, fetch, unpack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=['x86_64-linux-musl', 'aarch64-linux-musl', 'x86_64-macos-none', 'aarch64-macos-none'])
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--llvm-kit', type=Path)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('Release builds run on x86_64 Linux; native jobs test the archives')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    source = Path(__file__).resolve().parents[2]
    metadata_path = Path(__file__).with_name('release-inputs.json')
    metadata = json.loads(metadata_path.read_text())
    version = Path(__file__).with_name('version').read_text().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+\+acton\.[0-9]+', version):
        parser.error('Invalid release version')
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    downloads = work / 'downloads'
    spec = metadata['bootstrap_compiler']
    bootstrap = unpack(fetch(spec, downloads), work / 'bootstrap', spec['sha256'])
    zig = bootstrap / 'zig'
    if args.llvm_kit:
        llvm = args.llvm_kit.resolve()
        provenance_path = llvm / 'provenance.json'
        provenance = json.loads(provenance_path.read_text())
        if provenance['inputs_sha256'] != digest(metadata_path) or provenance['target'] != args.target:
            parser.error('LLVM kit inputs or target do not match this release')
        if provenance['recipe_sha256'] != digest(Path(__file__).with_name('build_llvm.py')):
            parser.error('LLVM kit recipe does not match this release')
        for name, expected in provenance['output_files'].items():
            if digest(llvm / name) != expected:
                parser.error('LLVM kit source hash mismatch: ' + name)
        kit_identity = {'provenance_sha256': digest(provenance_path), 'llvm_version': metadata['llvm_version']}
    else:
        if args.target not in metadata['devkits']:
            parser.error('This target requires --llvm-kit')
        spec = metadata['devkits'][args.target]
        llvm = unpack(fetch(spec, downloads), work / 'llvm', spec['sha256'])
        kit_identity = spec
    env = dict(os.environ)
    env.pop('ZIG_LIB_DIR', None)
    env['ZIG_LOCAL_CACHE_DIR'] = str(work / 'local-cache')
    env['ZIG_GLOBAL_CACHE_DIR'] = str(work / 'global-cache')
    actual = subprocess.check_output([zig, 'version'], env=env, text=True).strip()
    if actual != metadata['bootstrap_version']:
        parser.error('Unexpected bootstrap version: ' + actual)
    arch, system, _ = args.target.split('-')
    name = f'zig-{arch}-{system}-{version}'
    output = work / name
    if output.exists():
        parser.error('Output already exists: ' + str(output))
    target = args.target if system == 'linux' else f'{arch}-macos.15.0-none'
    command = [str(zig), 'build', '--zig-lib=lib', '--prefix', str(output),
               '--search-prefix', str(llvm), '--maxrss', '13000000000',
               '-j' + str(args.jobs), '-Dtarget=' + target, '-Dcpu=baseline',
               '-Doptimize=fast', '-Dstrip', '-Denable-llvm', '-Dstatic-llvm',
               '-Duse-llvm', '-Dflat', '-Dno-langref', '-Dversion-string=' + version]
    subprocess.run(command, cwd=source, env=env, check=True)
    for required in ('zig', 'lib/std/std.zig', 'lib/compiler/Maker.zig'):
        if not (output / required).is_file():
            parser.error('Missing installed file: ' + required)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    # Only archive this exact source revision; a dirty source tree cannot claim it.
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source, text=True)
    if dirty:
        parser.error('Release source tree has tracked modifications')
    provenance = {'version': version, 'source_commit': revision, 'target': target,
                  'bootstrap': metadata['bootstrap_compiler'], 'llvm_kit': kit_identity,
                  'inputs_sha256': digest(metadata_path), 'recipe_sha256': digest(Path(__file__)),
                  'binary_sha256': digest(output / 'zig'),
                  'library_sha256': {str(p.relative_to(output)): digest(p)
                    for p in sorted((output / 'lib').rglob('*')) if p.is_file()}}
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    shutil.copyfile(source / 'LICENSE', output / 'LICENSE')
    shutil.copyfile(source / 'README.md', output / 'README.md')
    archive = work / (name + '.tar.xz')
    subprocess.run(['tar', '--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner',
                    '-C', str(work), '-cJf', str(archive), name], check=True,
                   env={**env, 'XZ_OPT': '-T2 -6'})
    checksum = digest(archive)
    archive.with_suffix('.xz.sha256').write_text(checksum + '  ' + archive.name + '\n')
    print(json.dumps({'archive': str(archive), 'sha256': checksum, 'version': version, 'source_commit': revision}))


if __name__ == '__main__':
    main()
