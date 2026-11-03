#!/usr/bin/env python3
"""Build the missing Intel macOS static LLVM kit on Linux, once per input revision.

Uses the target stages of ziglang/zig-bootstrap, without rebuilding a host Zig
or host LLVM libraries. Only native TableGen executables are built from source.
Requires Python 3.12+, CMake, Ninja, and a host C/C++ compiler. The release build
uses this kit's include/ and lib/ with `zig build -Dstatic-llvm --search-prefix`.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess


from common import digest, fetch, unpack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path(__file__).with_name('release-inputs.json'))
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, help='Optional shared verified download cache')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--fetch-only', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    inputs = json.loads(args.inputs.read_text())
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    downloads = (args.downloads or work / 'downloads').resolve()
    downloads.mkdir(parents=True, exist_ok=True)
    source_spec, compiler_spec = inputs['bootstrap_source'], inputs['bootstrap_compiler']
    source_archive, compiler_archive = fetch(source_spec, downloads), fetch(compiler_spec, downloads)
    if args.fetch_only:
        print(json.dumps({'source': str(source_archive), 'compiler': str(compiler_archive)}, indent=2))
        return
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('Run this builder on an x86_64 Linux host; macOS runners only test the result')
    source = unpack(source_archive, work / ('source-' + source_spec['sha256'][:12]), source_spec['sha256'])
    compiler = unpack(compiler_archive, work / ('compiler-' + compiler_spec['sha256'][:12]), compiler_spec['sha256'])
    zig = compiler / 'zig'
    kit = inputs['built_devkit']
    target, cpu = kit['target'], kit['cpu']
    if target != 'x86_64-macos-none' or cpu != 'baseline':
        parser.error('This recipe is scoped to the Intel macOS baseline kit')
    prefix = work / kit['name']
    if prefix.exists():
        raise SystemExit(f'Output already exists; use a fresh work directory: {prefix}')
    identity = hashlib.sha256(args.inputs.read_bytes()).hexdigest()[:12]
    build = work / ('build-' + identity)
    build.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop('ZIG_LIB_DIR', None)
    env['ZIG_GLOBAL_CACHE_DIR'] = str(build / 'zig-global-cache')
    env['ZIG_LOCAL_CACHE_DIR'] = str(build / 'zig-local-cache')
    log = (build / 'commands.jsonl').open('a')

    def run(argv, cwd=None):
        argv = [str(a) for a in argv]
        print(shlex.join(argv), flush=True)
        log.write(json.dumps({'argv': argv, 'cwd': str(cwd or work)}) + '\n')
        log.flush()
        subprocess.run(argv, cwd=cwd or work, env=env, check=True)

    if subprocess.check_output([zig, 'version'], env=env, text=True).strip() != inputs['bootstrap_version']:
        raise SystemExit('Bootstrap compiler version differs from input manifest')
    host = build / 'tablegen'
    common = ['-DCMAKE_BUILD_TYPE=Release', '-DBUILD_SHARED_LIBS=OFF',
              '-DLLVM_APPEND_VC_REV=OFF', '-DLLVM_ENABLE_BINDINGS=OFF',
              '-DLLVM_ENABLE_BACKTRACES=OFF', '-DLLVM_ENABLE_CRASH_OVERRIDES=OFF',
              '-DLLVM_ENABLE_LIBEDIT=OFF', '-DLLVM_ENABLE_LIBPFM=OFF',
              '-DLLVM_ENABLE_LIBXML2=OFF', '-DLLVM_ENABLE_OCAMLDOC=OFF',
              '-DLLVM_ENABLE_PLUGINS=OFF', '-DLLVM_ENABLE_Z3_SOLVER=OFF',
              '-DLLVM_INCLUDE_TESTS=OFF', '-DLLVM_INCLUDE_EXAMPLES=OFF',
              '-DLLVM_INCLUDE_BENCHMARKS=OFF', '-DLLVM_INCLUDE_DOCS=OFF', '-DLLVM_INCLUDE_UTILS=OFF',
              '-DLLVM_TOOL_LLVM_LTO2_BUILD=OFF', '-DLLVM_TOOL_LLVM_LTO_BUILD=OFF',
              '-DLLVM_TOOL_LTO_BUILD=OFF', '-DLLVM_TOOL_REMARKS_SHLIB_BUILD=OFF',
              '-DCLANG_BUILD_TOOLS=OFF', '-DCLANG_INCLUDE_DOCS=OFF',
              '-DCLANG_INCLUDE_TESTS=OFF', '-DCLANG_TOOL_CLANG_IMPORT_TEST_BUILD=OFF',
              '-DCLANG_TOOL_CLANG_LINKER_WRAPPER_BUILD=OFF',
              '-DCLANG_TOOL_C_INDEX_TEST_BUILD=OFF', '-DCLANG_TOOL_LIBCLANG_BUILD=OFF']
    run(['cmake', '-S', source / 'llvm', '-B', host, '-G', 'Ninja', *common,
         '-DLLVM_ENABLE_PROJECTS=clang', '-DLLVM_ENABLE_ZLIB=OFF', '-DLLVM_ENABLE_ZSTD=OFF'])
    run(['cmake', '--build', host, '--target', 'llvm-tblgen', 'clang-tblgen', '--parallel', args.jobs])
    # CMake archiver settings accept paths, not Zig subcommands.
    wrappers = build / 'tools'
    wrappers.mkdir(exist_ok=True)
    for name in ('ar', 'ranlib'):
        path = wrappers / ('zig-' + name)
        path.write_text('#!/bin/sh\nexec ' + shlex.quote(str(zig)) + ' ' + name + ' "$@"\n')
        path.chmod(0o755)
    cross = [f'-DCMAKE_INSTALL_PREFIX={prefix}', f'-DCMAKE_PREFIX_PATH={prefix}',
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CROSSCOMPILING=True',
             '-DCMAKE_SYSTEM_NAME=Darwin',
             f'-DCMAKE_C_COMPILER={zig};cc;-fno-sanitize=all;-s;-target;{target};-mcpu={cpu}',
             f'-DCMAKE_CXX_COMPILER={zig};c++;-fno-sanitize=all;-s;-target;{target};-mcpu={cpu}',
             f'-DCMAKE_ASM_COMPILER={zig};cc;-fno-sanitize=all;-s;-target;{target};-mcpu={cpu}',
             '-DCMAKE_LINK_DEPENDS_USE_LINKER=OFF',
             f'-DCMAKE_AR={wrappers / "zig-ar"}', f'-DCMAKE_RANLIB={wrappers / "zig-ranlib"}']
    zlib = build / 'zlib'
    run(['cmake', '-S', source / 'zlib', '-B', zlib, '-G', 'Ninja', *cross, '-DZLIB_BUILD_EXAMPLES=OFF'])
    run(['cmake', '--build', zlib, '--target', 'install', '--parallel', args.jobs])
    (prefix / 'lib').mkdir(parents=True, exist_ok=True)
    (prefix / 'include' / 'zstd.h').write_bytes((source / 'zstd/lib/zstd.h').read_bytes())
    # Match the pinned upstream bootstrap source list exactly.
    zstd_names = '''decompress/zstd_ddict.c decompress/zstd_decompress.c decompress/huf_decompress.c
    decompress/huf_decompress_amd64.S decompress/zstd_decompress_block.c compress/zstdmt_compress.c
    compress/zstd_opt.c compress/hist.c compress/zstd_ldm.c compress/zstd_fast.c
    compress/zstd_compress_literals.c compress/zstd_double_fast.c compress/huf_compress.c
    compress/fse_compress.c compress/zstd_lazy.c compress/zstd_compress.c compress/zstd_compress_sequences.c
    compress/zstd_compress_superblock.c deprecated/zbuff_compress.c deprecated/zbuff_decompress.c
    deprecated/zbuff_common.c common/entropy_common.c common/pool.c common/threading.c common/zstd_common.c
    common/xxhash.c common/debug.c common/fse_decompress.c common/error_private.c dictBuilder/zdict.c
    dictBuilder/divsufsort.c dictBuilder/fastcover.c dictBuilder/cover.c'''.split()
    run([zig, 'build-lib', '--name', 'zstd', '-target', target, '-mcpu=' + cpu,
         '-fno-sanitize-c', '-fstrip', '-OReleaseFast', '-lc', '-j' + str(args.jobs),
         *[source / 'zstd/lib' / name for name in zstd_names]], cwd=prefix / 'lib')
    llvm = build / 'llvm-target'
    run(['cmake', '-S', source / 'llvm', '-B', llvm, '-G', 'Ninja', *common, *cross,
         '-DLLVM_ENABLE_PROJECTS=lld;clang', '-DLLVM_ENABLE_ZLIB=FORCE_ON',
         '-DLLVM_ENABLE_ZSTD=FORCE_ON', '-DLLVM_USE_STATIC_ZSTD=ON',
         f'-DLLVM_TABLEGEN={host / "bin/llvm-tblgen"}',
         f'-DCLANG_TABLEGEN={host / "bin/clang-tblgen"}',
         '-DLLVM_BUILD_UTILS=OFF', '-DLLVM_BUILD_TOOLS=OFF', '-DLLVM_BUILD_STATIC=ON',
         '-DLLVM_INCLUDE_UTILS=OFF', '-DLLVM_LINK_LLVM_DYLIB=OFF',
         f'-DLLVM_DEFAULT_TARGET_TRIPLE={target}', '-DCLANG_ENABLE_OBJC_REWRITER=ON',
         '-DLLD_BUILD_TOOLS=OFF'])
    run(['cmake', '--build', llvm, '--target', 'install', '--parallel', args.jobs])
    for required in ('include/llvm/IR/Module.h', 'include/clang/Frontend/CompilerInstance.h',
                     'lib/libLLVMCore.a', 'lib/libclangFrontend.a', 'lib/liblldCommon.a',
                     'lib/libz.a', 'lib/libzstd.a'):
        if not (prefix / required).is_file():
            raise SystemExit(f'Missing required static kit input: {required}')
    if any(p.suffix in ('.dylib', '.so') for p in prefix.rglob('*')):
        raise SystemExit('Static kit unexpectedly contains shared libraries')
    shutil.copytree(Path(__file__).with_name('notices'), prefix / 'third-party-notices')
    provenance = {'inputs': inputs, 'inputs_sha256': digest(args.inputs), 'recipe_sha256': digest(Path(__file__)),
                  'target': target, 'cpu': cpu, 'jobs': args.jobs,
                  'bootstrap_binary_sha256': digest(zig), 'output_files': {
                      str(p.relative_to(prefix)): digest(p) for p in sorted(prefix.rglob('*')) if p.is_file()}}
    (prefix / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    archive = work / (kit['name'] + '.tar.xz')
    run(['tar', '--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner',
         '-C', work, '-cJf', archive, prefix.name])
    checksum = digest(archive)
    archive.with_suffix(archive.suffix + '.sha256').write_text(checksum + '  ' + archive.name + '\n')
    print(json.dumps({'archive': str(archive), 'sha256': checksum, 'size': archive.stat().st_size}, indent=2))


if __name__ == '__main__':
    main()
