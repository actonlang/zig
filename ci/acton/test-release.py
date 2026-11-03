#!/usr/bin/env python3
"""Validate an official-layout Zig release archive on its native CI host."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback


class Failure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stop_group(process):
    # A leader can exit while one of its descendants still owns a pipe.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(process.pid, sig)
                break
            except ProcessLookupError:
                break
            except PermissionError:
                # macOS can report EPERM while the last group member exits.
                # Only accept a successful signal or ESRCH; persistent denial
                # must still fail cleanup.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    process.wait(timeout=2)


def clean_environment(work):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("ZIG_", "DYLD_")) or name in {
            "CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
            "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
            "LD_LIBRARY_PATH", "LD_PRELOAD",
        }:
            env.pop(name)
    env.update(ZIG_LOCAL_CACHE_DIR=str(work / "local-cache"),
               ZIG_GLOBAL_CACHE_DIR=str(work / "global-cache"),
               TMPDIR=str(work / "tmp"), LC_ALL="C")
    (work / "tmp").mkdir()
    return env


class Runner:
    def __init__(self, output, env, timeout, report):
        self.output, self.env, self.timeout, self.report = output, env, timeout, report

    def run(self, name, command, cwd, accepted=(0,), timeout=None):
        command = [str(arg) for arg in command]
        log = self.output / (name + ".log")
        started = time.monotonic()
        row = {"check": name, "command": command, "log": log.name}
        self.report["checks"].append(row)
        with log.open("wb") as stream:
            process = subprocess.Popen(command, cwd=cwd, env=self.env,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                row["exit_code"] = process.wait(timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                row["timed_out"] = True
                raise Failure(name + " timed out; see " + str(log))
            finally:
                stop_group(process)
                row["seconds"] = time.monotonic() - started
        require(row["exit_code"] in accepted, name + " failed; see " + str(log))
        require(log.stat().st_size < 16 * 1024 * 1024, name + " produced an oversized log")
        return log.read_text(errors="replace")


def check_layout(root):
    for name in ("zig", "lib/std/std.zig", "lib/compiler_rt.zig",
                 "lib/compiler/Maker.zig", "lib/include/stdint.h"):
        require((root / name).is_file(), "missing official-layout release file: " + name)
    require(os.access(root / "zig", os.X_OK), "zig is not executable")
    for path in root.rglob("*"):
        if path.is_symlink():
            require(not os.path.isabs(os.readlink(path)), "absolute release symlink: " + str(path))
            require(os.path.commonpath((str(root), str(path.resolve()))) == str(root),
                    "release symlink leaves the archive: " + str(path))


def unpack_archive(archive, output):
    checksum = Path(str(archive) + ".sha256")
    fields = checksum.read_text().strip().split()
    require(len(fields) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]) is not None,
            "invalid archive checksum file: " + str(checksum))
    require(fields[1].lstrip("*") == archive.name, "checksum names a different archive")
    require(sha256(archive) == fields[0].lower(), "archive checksum mismatch")
    unpacked = output / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archive, "r:xz") as tar:
        members = tar.getmembers()
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and ".." not in path.parts and path.parts,
                    "unsafe archive path: " + member.name)
            # The release packager copies the installed tree into its official
            # layout. Keep the archive independent of filesystem link targets.
            require(member.isdir() or member.isfile(), "non-regular release archive entry: " + member.name)
            roots.add(path.parts[0])
        require(len(roots) == 1, "release archive must have exactly one top directory")
        tar.extractall(unpacked, members=members)
    return unpacked / roots.pop(), fields[0].lower()


def check_provenance(root, source, expected_version):
    provenance = json.loads((root / "provenance.json").read_text())
    require(provenance.get("binary_sha256") == sha256(root / "zig"), "provenance binary hash mismatch")
    library = provenance.get("library_sha256")
    require(isinstance(library, dict) and library, "provenance must contain per-file library_sha256 hashes")
    actual = {p.relative_to(root).as_posix(): sha256(p)
              for p in sorted((root / "lib").rglob("*")) if p.is_file()}
    require(library == actual, "provenance library manifest differs from packaged lib/")
    notices = provenance.get("notices_sha256")
    require(isinstance(notices, dict) and notices, "provenance must contain redistribution notice hashes")
    actual_notices = {p.relative_to(root).as_posix(): sha256(p)
                     for p in sorted((root / "third-party-notices").rglob("*")) if p.is_file()}
    require(notices == actual_notices, "redistribution notices differ from provenance")
    if source:
        expected_notices = {"third-party-notices/" + p.relative_to(source / "ci/acton/notices").as_posix(): sha256(p)
                            for p in sorted((source / "ci/acton/notices").rglob("*")) if p.is_file()}
        require(notices == expected_notices, "redistribution notices differ from --source checkout")
    commit = provenance.get("source_commit", "")
    require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "invalid provenance source commit")
    if source:
        checkout = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                           text=True, timeout=20).strip()
        require(checkout == commit, "archive source commit differs from --source checkout HEAD")
        for name, digest in actual.items():
            path = source / name
            require(path.is_file(), "packaged library file is absent from --source checkout: " + name)
            # Match build.zig's installDirectory blank_extensions policy.
            expected = hashlib.sha256(b"").hexdigest() if name.endswith("test.zig") else sha256(path)
            require(expected == digest, "packaged library differs from --source checkout: " + name)
    if "version" in provenance:
        require(provenance["version"] == expected_version, "provenance version mismatch")
    require(isinstance(provenance.get("target"), str), "missing provenance target")
    return {"source_commit": commit, "target": provenance["target"],
            "binary_sha256": provenance["binary_sha256"],
            "library_files": len(actual), "provenance_sha256": sha256(root / "provenance.json")}


def check_host_target(zig, target, arch, host_os):
    parts = target.split("-")
    require(len(parts) >= 2 and parts[0] == arch and parts[1].split(".")[0] == host_os,
            "provenance target does not match this host: " + target)
    with zig.open("rb") as stream:
        header = stream.read(32)
    require(len(header) == 32, "truncated compiler binary")
    if host_os == "macos":
        require(header[:4] == b"\xcf\xfa\xed\xfe", "expected a thin 64-bit little-endian Mach-O compiler")
        actual = {0x01000007: "x86_64", 0x0100000c: "aarch64"}.get(struct.unpack_from("<I", header, 4)[0])
    else:
        require(header[:6] == b"\x7fELF\x02\x01", "expected a 64-bit little-endian ELF compiler")
        actual = {62: "x86_64", 183: "aarch64"}.get(struct.unpack_from("<H", header, 18)[0])
    require(actual == arch, "compiler binary architecture differs from native CI host: " + str(actual))


def check_linkage(runner, zig, cwd):
    if sys.platform == "darwin":
        output = runner.run("host-linkage", ["otool", "-L", zig], cwd)
        libraries = [line.strip().split(" (", 1)[0] for line in output.splitlines()[1:] if line.strip()]
        require(bool(libraries), "otool did not report the Mach-O system dependencies")
        for library in libraries:
            require(library.startswith(("/usr/lib/", "/System/Library/Frameworks/")),
                    "non-system Mach-O dependency: " + library)
            require(not re.search(r"llvm|clang|libz3|libzstd", library, re.I),
                    "shared toolchain dependency: " + library)
        return libraries
    # Linux release compilers use a static musl host target. ldd alone cannot
    # establish this: inspect both the loader and dynamic dependency tables.
    output = runner.run("host-linkage", ["ldd", zig], cwd, accepted=(0, 1))
    require("statically linked" in output or "not a dynamic executable" in output,
            "Linux release compiler is dynamically linked; see host-linkage.log")
    segments = runner.run("host-segments", ["readelf", "-lW", zig], cwd)
    dynamic = runner.run("host-dynamic", ["readelf", "-dW", zig], cwd)
    require(not re.search(r"^\s*INTERP\s", segments, re.M), "Linux compiler has a PT_INTERP loader")
    require("(NEEDED)" not in dynamic, "Linux compiler has shared-library dependencies")
    return []


ZIG_SOURCE = '''extern "c" fn atoi([*:0]const u8) c_int;
extern "c" fn printf([*:0]const u8, ...) c_int;
pub fn main() void {
    const result = atoi("7") * 6;
    if (result != 42) @panic("bad Zig result");
    if (printf("zig-runtime=%d\\n", result) < 0) @panic("stdout failed");
}
'''
C_SOURCE = '''#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    if (argc != 2) return 1;
    int result = atoi(argv[1]) * 6;
    if (result != 42) return 2;
    printf("c-runtime=%d\\n", result);
    return 0;
}
'''
CPP_SOURCE = '''#include <cstdio>
#include <cstdlib>
#include <numeric>
#include <vector>
int main(int argc, char **argv) {
    if (argc != 2) return 1;
    std::vector<int> values(6, std::atoi(argv[1]));
    int result = std::accumulate(values.begin(), values.end(), 0);
    if (result != 42) return 2;
    std::printf("cpp-runtime=%d\\n", result);
    return 0;
}
'''


def runtime_checks(runner, zig, work, target):
    for name, suffix, source in (("zig", "zig", ZIG_SOURCE), ("c", "c", C_SOURCE),
                                 ("cpp", "cpp", CPP_SOURCE)):
        path = work / (name + "." + suffix)
        path.write_text(source)
        binary = work / (name + "-program")
        command = [zig, "build-exe", path, "-fllvm", "-Odebug", "-lc", "-femit-bin=" + str(binary)]
        if target:
            command += ["-target", target]
        if name == "cpp":
            command += ["-lc++"]
        runner.run(name + "-compile", command, work)
        output = runner.run(name + "-execute", [binary] + ([] if name == "zig" else ["7"]), work, timeout=20)
        require(output == name + "-runtime=42\n", name + " returned incorrect runtime output")


BSP_BUILD = '''const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const native = b.option(bool, "native", "Exercise native C compiler updates") orelse false;
    const exe = b.addExecutable(.{
        .name = "bsp-probe",
        .root_module = b.createModule(.{ .target = target, .optimize = .debug, .link_libc = true }),
        .use_llvm = !native,
    });
    exe.incremental = native;
    if (target.result.os.tag == .macos) exe.use_new_linker = false;
    exe.root_module.addCSourceFile(.{ .file = b.path("main.c"), .flags = &.{} });
    exe.root_module.addIncludePath(b.path("."));
    b.installArtifact(exe);
}
'''


class BuildServer:
    """Minimal client for the pinned Zig build-system protocol, version 1."""
    def __init__(self, runner, zig, work, target, backend):
        self.runner, self.work, self.backend = runner, work, backend
        self.step = None
        self.configurations = 0
        self.log = (runner.output / ("bsp-" + backend + "-stderr.log")).open("wb")
        command = [str(zig), "build", "--listen=-", "--prefix", str(work / "out"), "-j2",
                   "-Dnative=" + ("true" if backend == "native" else "false")]
        if target:
            command.append("-Dtarget=" + target)
        try:
            self.process = subprocess.Popen(command, cwd=work, env=runner.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log, bufsize=0, start_new_session=True)
        except BaseException:
            self.log.close()
            raise

    def read(self, count, deadline):
        result = bytearray()
        while len(result) < count:
            seconds = deadline - time.monotonic()
            require(seconds > 0, "BSP request timed out; see " + self.log.name)
            require(bool(select.select([self.process.stdout], [], [], seconds)[0]),
                    "BSP request timed out; see " + self.log.name)
            data = os.read(self.process.stdout.fileno(), count - len(result))
            require(bool(data), "BSP closed its protocol stream; see " + self.log.name)
            result.extend(data)
        return bytes(result)

    def frame(self, deadline):
        tag, size = struct.unpack("<II", self.read(8, deadline))
        require(size <= 64 * 1024 * 1024, "oversized BSP frame")
        return tag, self.read(size, deadline)

    def configure(self, body):
        path = self.work / os.fsdecode(body)
        with path.open("rb") as stream:
            header = stream.read(44)
        require(len(header) == 44, "truncated BSP configuration header")
        # std.Build.Configuration.Header stores eight lengths, then default_step.
        self.step = struct.unpack_from("<I", header, 32)[0]
        require(self.step < struct.unpack_from("<I", header, 4)[0], "invalid default build step")
        self.configurations += 1

    def start(self):
        deadline = time.monotonic() + self.runner.timeout
        handshake = False
        while self.step is None:
            tag, body = self.frame(deadline)
            if tag == 0x80000000:
                require(len(body) >= 8 and struct.unpack_from("<I", body)[0] == 1,
                        "unsupported BSP protocol version")
                handshake = True
            elif tag == 0x80000001:
                require(handshake, "BSP configuration arrived before handshake")
                self.configure(body)
            elif tag == 0x80000002:
                raise Failure("BSP initial configuration failed; see " + self.log.name)

    def request(self):
        self.process.stdin.write(struct.pack("<IIIII", 0x80000000, 12, 1, 0, self.step))
        self.process.stdin.flush()

    def build(self, phase, expected):
        deadline = time.monotonic() + self.runner.timeout
        self.request()
        failed, diagnostics, steps = False, [], 0
        while True:
            tag, body = self.frame(deadline)
            if tag == 0x80000001:
                self.configure(body)
                require(self.configurations <= 8, "BSP repeatedly reconfigured a fixed graph")
                self.request()
            elif tag == 0x80000002:
                raise Failure("BSP configuration failed during " + phase)
            elif tag == 0x80000006:
                require(len(body) >= 20, "truncated BSP step result")
                _, status, extra_len, strings_len, _ = struct.unpack_from("<IIIII", body)
                require(status in (0, 1, 2, 3), "unknown BSP step status")
                start = 20 + 4 * extra_len
                require(start + strings_len <= len(body), "truncated BSP error strings")
                diagnostics.append(body[start:start + strings_len].decode(errors="replace"))
                failed |= status in (1, 3)
                steps += 1
            elif tag == 0x80000004:
                require(not body, "unexpected BSP completion body")
                break
        require(steps > 0, "BSP returned no step results")
        if expected is None:
            require(failed, "invalid header did not fail the BSP generation")
            require("RELEASE_VALIDATION_ERROR" in "\n".join(diagnostics), "BSP lost the C diagnostic")
        else:
            require(not failed, "BSP generation failed: " + "\n".join(diagnostics))
            output = self.runner.run("bsp-" + self.backend + "-" + phase,
                                     [self.work / "out/bin/bsp-probe"], self.work, timeout=20)
            require(output == "bsp=" + str(expected) + "\n", "stale or incorrect BSP executable")
        require(self.process.poll() is None, "BSP server exited")
        return {"phase": phase, "pid": self.process.pid, "expected": expected, "failed": failed,
                "compiler_pids": self.compiler_pids()}

    def compiler_pids(self):
        # Observe only this Maker's descendants after completion. Fresh-process
        # steps have already waited for their compiler; retained steps keep it.
        output = subprocess.check_output(["ps", "-axww", "-o", "pid=,ppid=,command="], text=True, timeout=20)
        processes = {}
        for line in output.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3:
                processes[int(fields[0])] = (int(fields[1]), fields[2])
        descendants = {self.process.pid}
        while True:
            added = {pid for pid, (parent, _) in processes.items() if parent in descendants} - descendants
            if not added:
                break
            descendants.update(added)
        return sorted(pid for pid in descendants if pid in processes and
                      "build-exe " in processes[pid][1] and "--listen=-" in processes[pid][1] and
                      "bsp-probe" in processes[pid][1])

    def close(self):
        try:
            stop_group(self.process)
        finally:
            self.process.stdin.close()
            self.process.stdout.close()
            self.log.close()


def bsp_checks(runner, zig, work, target):
    work.mkdir()
    lanes = {}
    runner.report["bsp"] = lanes
    for backend in ("llvm", "native"):
        lane = work / backend
        lane.mkdir()
        (lane / "build.zig").write_text(BSP_BUILD)
        (lane / "main.c").write_text('#include <stdio.h>\n#include "value.h"\nint main(void) { printf("bsp=%d\\n", VALUE); return 0; }\n')
        header = lane / "value.h"
        header.write_text("#define VALUE 41\n")
        server = BuildServer(runner, zig, lane, target, backend)
        rows = []
        lanes[backend] = rows
        try:
            server.start()
            for phase, value in (("initial", 41), ("updated", 42), ("error", None), ("recovered", 43)):
                header.write_text("#error RELEASE_VALIDATION_ERROR\n" if value is None else "#define VALUE " + str(value) + "\n")
                rows.append(server.build(phase, value))
        finally:
            server.close()
        require(len({row["pid"] for row in rows}) == 1, "BSP server restarted")
        if backend == "native" and sys.platform == "darwin":
            require(all(len(row["compiler_pids"]) == 1 for row in rows), "native Mach-O compiler was not retained")
            require(len({row["compiler_pids"][0] for row in rows}) == 1, "native Mach-O compiler restarted")
        else:
            require(all(not row["compiler_pids"] for row in rows), "unsupported C-bearing compiler was retained")
    return lanes


def source_checks(runner, zig, source, work, mac_arm):
    lib = zig.parent / "lib"
    output = runner.run("cache-tests", [zig, "test", lib / "std/std.zig", "-fllvm",
        "--test-filter", "Build.Cache"], work)
    require("depfile promotion preserves the observed content snapshot" in output,
            "release library did not run the required cache regression")
    fixtures = ["c_include_cache", "c_runtime_rebuild"]
    if mac_arm:
        fixtures += ["macho_incremental_inputs"]
    if platform.machine().lower() in {"aarch64", "arm64"}:
        fixtures += ["aarch64_slice_field", "aarch64_padded_aggregate"]
    for fixture in fixtures:
        original = source / "test/standalone" / fixture
        require((original / "build.zig").is_file(), "missing source regression: " + fixture)
        copied = work / fixture
        shutil.copytree(original, copied, ignore=shutil.ignore_patterns(".zig-cache", "zig-out", "zig-pkg"))
        runner.run(fixture, [zig, "build", "-j2", "--summary", "all"], copied)
    if mac_arm:
        abi = work / "c-abi"
        abi.mkdir()
        for name in ("regressions.zig", "regressions.c", "assert_or_panic.h", "regressions_runner.zig"):
            shutil.copy2(source / "test/c_abi" / name, abi / name)
        filters = ("conditional early return through C", "external global offsets through C",
                   "indirect result pointer under register pressure")
        for backend in ("llvm", "no-llvm"):
            command = [zig, "test", abi / "regressions.zig", abi / "regressions.c", "-lc",
                       "--test-runner", abi / "regressions_runner.zig",
                       "-f" + backend, "-fno-new-linker", "-Odebug", "-j2"]
            for name in filters:
                command += ["--test-filter", name]
            output = runner.run("c-abi-" + backend, command, abi)
            for name in filters:
                require(name in output, "C ABI regression did not run: " + name)
    return {"cache": "passed", "standalone": fixtures,
            "aarch64_macos_abi": "LLVM and native passed" if mac_arm else "not applicable on this host"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", nargs="?", type=Path, help="unpacked top directory containing zig and lib/")
    parser.add_argument("--archive", type=Path, help="release .tar.xz with an adjacent .sha256 file")
    parser.add_argument("--version", "--expected-version", dest="expected_version", required=True)
    parser.add_argument("--target", help="optional executable target; must match this host OS/architecture")
    parser.add_argument("--source", type=Path, help="matching patched Zig source checkout for regression drivers")
    parser.add_argument("--output", type=Path, help="new evidence directory (default: a retained temporary directory)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per command or complete BSP request")
    args = parser.parse_args()
    if (args.release is None) == (args.archive is None):
        parser.error("provide either an unpacked release directory or --archive")
    if sys.platform not in ("linux", "darwin") or args.timeout <= 0:
        parser.error("requires Linux/macOS and a positive timeout")
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    host_os = "macos" if sys.platform == "darwin" else "linux"
    if args.target and args.target != "native":
        parts = args.target.split("-")
        if len(parts) < 2 or parts[0] != arch or parts[1].split(".")[0] != host_os:
            parser.error("--target must produce binaries executable on this CI host")
    target = None if args.target == "native" else args.target
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="zig-release-check-"))
    if args.output:
        output.mkdir(parents=True, exist_ok=False)
    report = {"release": str((args.release or args.archive).resolve()), "expected_version": args.expected_version,
              "host": platform.platform(), "target": target, "checks": [], "failure": None}
    try:
        if args.archive:
            original, report["archive_sha256"] = unpack_archive(args.archive.resolve(), output)
        else:
            original = args.release.resolve()
        check_layout(original)
        report["provenance"] = check_provenance(original, args.source, args.expected_version)
        check_host_target(original / "zig", report["provenance"]["target"], arch, host_os)
        relocated = output / "relocated toolchain"
        if args.archive:
            original.rename(relocated)
        else:
            shutil.copytree(original, relocated, symlinks=True)
        work = output / "fresh work"
        work.mkdir()
        runner = Runner(output, clean_environment(work), args.timeout, report)
        zig = relocated / "zig"
        report["compiler_sha256"] = sha256(zig)
        report["libraries"] = check_linkage(runner, zig, work)
        version = runner.run("version", [zig, "version"], work).strip()
        require(version == args.expected_version, "version mismatch: " + version)
        report["version"] = version
        runtime_checks(runner, zig, work, target)
        report["bsp"] = bsp_checks(runner, zig, work / "bsp", target)
        if args.source:
            report["source_tests"] = source_checks(runner, zig, args.source.resolve(), work,
                                                    host_os == "macos" and arch == "aarch64")
        else:
            report["source_tests"] = "not requested"
    except (Failure, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        report["failure"] = str(error)
        report["failure_traceback"] = traceback.format_exc()
    finally:
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(("FAIL: " + report["failure"] if report["failure"] else "PASS") + "; evidence: " + str(output))
    return int(report["failure"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
