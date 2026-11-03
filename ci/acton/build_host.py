#!/usr/bin/env python3
"""Bootstrap a source-matched Linux compiler through upstream CMake's zig2 target."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
from common import digest, fetch, unpack


TARGET = 'x86_64-linux-musl'


def cmake_command(source, build, zig, llvm, version, wrappers):
    return ['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja',
            '-DCMAKE_BUILD_TYPE=Release',
            '-DCMAKE_PREFIX_PATH=' + str(llvm),
            '-DCMAKE_C_COMPILER=' + str(zig) + ';cc;-target;' + TARGET + ';-mcpu=baseline',
            '-DCMAKE_CXX_COMPILER=' + str(zig) + ';c++;-target;' + TARGET + ';-mcpu=baseline',
            '-DCMAKE_AR=' + str(wrappers / 'zig-ar'),
            '-DCMAKE_RANLIB=' + str(wrappers / 'zig-ranlib'),
            '-DZIG_TARGET_TRIPLE=' + TARGET, '-DZIG_HOST_TARGET_TRIPLE=' + TARGET,
            '-DZIG_TARGET_MCPU=baseline', '-DZIG_STATIC=ON', '-DZIG_NO_LIB=ON',
            '-DZIG_USE_LLVM_CONFIG=OFF', '-DZIG_VERSION=' + version]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('Host bootstrap builds run on x86_64 Linux')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    source = Path(__file__).resolve().parents[2]
    inputs_path = Path(__file__).with_name('release-inputs.json')
    inputs = json.loads(inputs_path.read_text())
    version = Path(__file__).with_name('version').read_text().strip()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source, text=True):
        parser.error('Host bootstrap requires a source tree without tracked modifications')
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output = work / 'host-bootstrap'
    if output.exists():
        parser.error('Output already exists: ' + str(output))
    identity = {'source_commit': revision, 'inputs_sha256': digest(inputs_path),
                'recipe_sha256': digest(Path(__file__)), 'version': version}
    identity_path = work / 'build-inputs.json'
    if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
        parser.error('Work directory belongs to different source or build inputs')
    identity_path.write_text(json.dumps(identity, indent=2) + '\n')
    downloads = work / 'downloads'
    compiler_spec = inputs['bootstrap_compiler']
    compiler = unpack(fetch(compiler_spec, downloads), work / 'bootstrap', compiler_spec['sha256'])
    kit_spec = inputs['devkits'][TARGET]
    llvm = unpack(fetch(kit_spec, downloads), work / 'llvm', kit_spec['sha256'])
    zig = compiler / 'zig'
    env = dict(os.environ)
    env.pop('ZIG_LIB_DIR', None)
    env['ZIG_LOCAL_CACHE_DIR'] = str(work / 'local-cache')
    env['ZIG_GLOBAL_CACHE_DIR'] = str(work / 'global-cache')
    actual = subprocess.check_output([zig, 'version'], env=env, text=True).strip()
    if actual != inputs['bootstrap_version']:
        parser.error('Unexpected bootstrap compiler version: ' + actual)
    wrappers = work / 'tools'
    wrappers.mkdir(exist_ok=True)
    for name in ('ar', 'ranlib'):
        wrapper = wrappers / ('zig-' + name)
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(str(zig)) + ' ' + name + ' "$@"\n')
        wrapper.chmod(0o755)

    def run(name, command):
        command = list(map(str, command))
        print(shlex.join(command), flush=True)
        with (work / 'commands.jsonl').open('a') as commands:
            commands.write(json.dumps({'command': command, 'cwd': str(source)}) + '\n')
        with (work / (name + '.log')).open('wb') as log:
            with subprocess.Popen(command, cwd=source, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT) as process:
                while data := process.stdout.read1(64 * 1024):
                    log.write(data)
                    log.flush()
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                result = process.wait()
            if result:
                raise subprocess.CalledProcessError(result, command)

    build = work / 'build'
    run('configure', cmake_command(source, build, zig, llvm, version, wrappers))
    # CMake translates the checked-in WASM bootstrap to C, then builds the
    # current compiler as C. Do not invoke an older compiler's build runner.
    run('build', ['cmake', '--build', build, '--target', 'zig2', '--parallel', args.jobs])
    binary = build / 'zig2'
    # zig2 deliberately omits the version command. Check its embedded version
    # through compilation, then exercise the matching configure protocol.
    smoke = work / 'smoke.zig'
    smoke.write_text('const std = @import("std");\n'
                     'comptime { if (!std.mem.eql(u8, @import("builtin").zig_version_string, '
                     + json.dumps(version) + ')) @compileError("bootstrap version mismatch"); }\n'
                     'pub fn main() void {}\n')
    run('smoke', [binary, 'run', '--zig-lib-dir', source / 'lib', smoke, '-fllvm', '-lc'])
    run('configure-smoke', [binary, 'build', '--zig-lib=lib', '--help'])
    segments = subprocess.check_output(['readelf', '-lW', binary], text=True)
    dynamic = subprocess.check_output(['readelf', '-dW', binary], text=True)
    if re.search(r'^\s*INTERP\s', segments, re.M) or '(NEEDED)' in dynamic:
        parser.error('Host bootstrap compiler has unexpected dynamic dependencies')
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() != revision or subprocess.check_output(
            ['git', 'status', '--porcelain', '--untracked-files=no'], cwd=source, text=True):
        parser.error('Release source changed while building the host bootstrap')
    output.mkdir()
    shutil.copy2(binary, output / 'zig')
    shutil.copyfile(source / 'LICENSE', output / 'LICENSE')
    notices = output / 'third-party-notices'
    shutil.copytree(Path(__file__).with_name('notices'), notices)
    # Zig CC links these runtimes from the pinned bootstrap installation.
    for name in ('libc/musl/COPYRIGHT', 'libcxx/LICENSE.TXT',
                 'libcxxabi/LICENSE.TXT', 'libunwind/LICENSE.TXT'):
        destination = notices / 'bootstrap-runtime' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(compiler / 'lib' / name, destination)
    provenance = {**identity, 'binary_sha256': digest(output / 'zig'),
                  'target': TARGET, 'bootstrap_compiler': compiler_spec, 'llvm_kit': kit_spec,
                  'notices_sha256': {str(p.relative_to(output)): digest(p)
                    for p in sorted(notices.rglob('*')) if p.is_file()}}
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    archive = work / 'host-bootstrap.tar.xz'
    run('archive', ['tar', '--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner',
                    '-C', work, '-cJf', archive, output.name])
    checksum = digest(archive)
    archive.with_suffix('.xz.sha256').write_text(checksum + '  ' + archive.name + '\n')
    print(json.dumps({'archive': str(archive), 'sha256': checksum, **identity}))


if __name__ == '__main__':
    main()
