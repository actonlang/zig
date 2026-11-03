# Zig releases for Acton

This branch follows upstream Zig and carries the compiler, cache, linker and
build-runner changes needed by Acton. The release contains the executable and
its matching `lib/` tree. Acton does not rebuild Zig or apply these patches again.

`release-inputs.json` pins the bootstrap compiler, the official static LLVM
kits and the source archive used for the Intel macOS kit. Every download is
checked before extraction. `version` names the next immutable release.

The Release builds workflow first uses upstream's CMake bootstrap to build
`zig2` from the checked-in `stage1/zig1.wasm` and current sources. This host
compiler and its current library share the same build protocol and APIs, even
when they have changed since the latest downloadable nightly. The pinned
nightly supplies C/C++ compilation for this bootstrap and the Intel LLVM kit.
The host bootstrap is an internal CI artifact, not a release package.

That host compiler then builds four release packages on Linux: x86_64 and
AArch64 Linux, and x86_64 and AArch64 macOS. Linux packages target musl and do
not depend on the build host's glibc. macOS packages target macOS 15. LLVM,
Clang, LLD and their support libraries are linked statically. Each archive is
then extracted into a new directory and tested on its own host architecture.

The build jobs use ordinary public Ubuntu runners with 16GB RAM and two build
jobs. The macOS runners only execute package tests; their smaller memory does
not limit compiler construction. The missing Intel macOS LLVM kit is built
from the pinned upstream bootstrap sources and cached by inputs and recipe.
Changing either invalidates the cache. Initial LLVM builds can take hours;
subsequent compiler releases reuse the kit. New source revisions keep an
in-progress dependency build running; kit jobs serialize and restore its cache
once it finishes.

The build server stays alive across build requests. C-bearing steps retain
their compiler only with the native backend and old Mach-O linker, which can
refresh C inputs. Other C-bearing steps use a fresh compiler for each request.
This avoids stale C output on linkers that do not implement those updates.

The AArch64 macOS ABI checks reuse the three cases in
`test/c_abi/regressions.zig` and their C helpers. The normal C ABI suite imports
these same cases. Release checks use a small C-entry test runner because the
broader native test harness and unrelated exported ABI functions still cause
excessive compiler memory use on this revision. The focused checks run all
three unchanged test bodies with both LLVM and the native backend; they do
not claim that the full native C ABI suite passes. Forwarding an over-aligned
one-field struct by value to C also still crashes the native compiler. The
`aarch64_padded_aggregate` fixture covers copying these values through pointers,
which is the repaired case.

To prepare a release:

1. Fetch upstream and rebase the compiler fixes. Keep independent fixes in
   separate commits, and drop fixes that upstream already contains.
2. Update the pinned build inputs if required, and give `version` a new value.
   Never replace a published version or its compiler/library sources.
3. Push this branch and inspect all native package tests.
4. Dispatch Release builds with `publish` enabled. It publishes only after
   all four packages pass. The release starts as a draft and becomes public
   only after all archives, SHA256SUMS and release.json have been uploaded.
5. Update Acton's release pin and its four checked-in archive hashes together.

The GitHub release tag is `acton-` followed by the version. Packages retain
Zig's archive naming and layout, so an Acton build only needs the pinned URL
and digest. `release.json` records the source commit; each archive also carries
its compiler, library and dependency provenance.

The scripts require Python 3.12+, CMake and Ninja on x86_64 Linux:

```
python3 ci/acton/build_host.py --work-dir /tmp/zig-host --jobs 2
python3 ci/acton/build_release.py --host-bootstrap /tmp/zig-host/host-bootstrap.tar.xz --target aarch64-macos-none --work-dir /tmp/zig-release
python3 ci/acton/build_llvm.py --work-dir /tmp/zig-llvm --jobs 2
```

The last command builds the Intel macOS dependency kit. Pass its output
folder as `--llvm-kit` when building `x86_64-macos-none`.
