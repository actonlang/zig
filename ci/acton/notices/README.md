# Third-party notices

The Zig compiler links LLVM, Clang, LLD, Zstandard and zlib statically. Their
licenses and the additional notices from LLVM Support and Zstandard are included
here. Zig's license is in the archive's root `LICENSE`; the bundled runtime and
standard-library sources retain their own notices in `lib/`.

The official Zig devkits pinned in `ci/acton/release-inputs.json` contain LLVM,
Clang and LLD 23.1.2. The Intel macOS kit built from the pinned Zig bootstrap
archive uses 23.1.4. The LLVM notices here were checked against both upstream
release tags and are identical in those versions. All four kits use Zstandard
1.5.2 and zlib 1.3.1. The 23.1.4, Zstandard and zlib notices also match the pinned
bootstrap sources.

`sources.json` records the upstream URL, source hash, selected line range and
installed notice hash for each file. Whole license files and selected comment
blocks are preserved verbatim. Zstandard's BSD license is included together with
its xxHash attribution and libdivsufsort notice. LLVM's project licenses are
included together with its BLAKE3, regular-expression, strlcpy, xxHash and Unicode
notices. LLVM's versions include changes to the regular-expression and Unicode
implementations.

Review these notices when changing the pinned dependency versions. LLVM and
Clang changes made in the bootstrap source archive are listed in that archive's
README; this Zig fork's compiler changes are recorded by the release source commit.
