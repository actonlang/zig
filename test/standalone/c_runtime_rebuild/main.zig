const std = @import("std");
const builtin = @import("builtin");
const Io = std.Io;
const Mode = enum { whole, incremental };
const Language = enum { c, cpp };

pub fn main(init: std.process.Init) !void {
    const arena = init.arena.allocator();
    const io = init.io;
    const args = try init.minimal.args.toSlice(arena);
    if (args.len != 4) return error.BadArguments;
    const zig = try Io.Dir.cwd().realPathFileAlloc(io, args[1], arena);
    const zig_lib = try Io.Dir.cwd().realPathFileAlloc(io, args[2], arena);
    const scratch = try Io.Dir.cwd().realPathFileAlloc(io, args[3], arena);
    const global_cache = try std.fs.path.join(arena, &.{ scratch, "global-cache" });
    for (std.enums.values(Language)) |language| {
        for (std.enums.values(Mode)) |mode| {
            // The old Mach-O linker supports replacing retained C inputs.
            if (mode == .incremental and builtin.os.tag != .macos) continue;
            const root = try std.fs.path.join(arena, &.{ scratch, @tagName(language), @tagName(mode) });
            const dir = try Io.Dir.cwd().createDirPathOpen(io, root, .{});
            defer dir.close(io);
            const filename = if (language == .c) "main.c" else "main.cpp";
            try dir.writeFile(io, .{ .sub_path = filename, .data = if (language == .c)
                \\#include <stdio.h>
                \\#include "value.h"
                \\int main(void) { puts("C runtime"); return VALUE; }
                \\
            else
                \\#include <vector>
                \\#include "value.h"
                \\int main(void) {
                \\    std::vector<int> values(1, VALUE);
                \\    try { throw values.at(0); }
                \\    catch (int value) { return value; }
                \\}
                \\
            });
            try dir.writeFile(io, .{ .sub_path = "value.h", .data = "#define VALUE 41\n" });
            var command: std.ArrayList([]const u8) = .empty;
            try command.appendSlice(arena, &.{ zig, "build-exe", filename, "-lc", "-fno-compiler-rt", "-fno-sanitize-c", "--zig-lib-dir", zig_lib, "--cache-dir", "cache", "--global-cache-dir", global_cache, "--name", "program", "--listen=-" });
            try command.append(arena, if (mode == .whole) "-fno-incremental" else "-fincremental");
            if (language == .cpp) try command.append(arena, "-lc++");
            if (builtin.os.tag == .macos) try command.appendSlice(arena, &.{ "-fno-llvm", "-fno-new-linker", "--verbose-link" });
            try retained(arena, io, command.items, root, dir, mode, language);
            try fresh(arena, io, command.items, root);
        }
    }
}

fn retained(arena: std.mem.Allocator, io: Io, command: []const []const u8, root: []const u8, dir: Io.Dir, mode: Mode, language: Language) !void {
    const log = try dir.createFile(io, "compiler.log", .{});
    defer log.close(io);
    var child = try std.process.spawn(io, .{
        .argv = command,
        .cwd = .{ .path = root },
        .stdin = .pipe,
        .stdout = .pipe,
        .stderr = .{ .file = log },
    });
    defer child.kill(io);
    var read_buffer: [4096]u8 = undefined;
    var write_buffer: [64]u8 = undefined;
    var reader = child.stdout.?.readerStreaming(io, &read_buffer);
    var writer = child.stdin.?.writerStreaming(io, &write_buffer);
    const client: std.zig.Client = .{ .in = &reader.interface, .out = &writer.interface };
    try run(arena, io, root, (try update(arena, client)).digest, 41);
    const unchanged = try update(arena, client);
    if (mode == .whole and !unchanged.cache_hit) return error.ExpectedCacheHit;
    try run(arena, io, root, unchanged.digest, 41);
    try dir.writeFile(io, .{ .sub_path = "value.h", .data = "#define VALUE 42\n" });
    try run(arena, io, root, (try update(arena, client)).digest, 42);
    try dir.writeFile(io, .{ .sub_path = "value.h", .data = "#error deliberate failure\n" });
    try std.testing.expectError(error.CompilationFailed, update(arena, client));
    try dir.writeFile(io, .{ .sub_path = "value.h", .data = "#define VALUE 43\n" });
    try run(arena, io, root, (try update(arena, client)).digest, 43);
    try client.serveBodylessMessage(.exit);
    if (!(try child.wait(io)).eql(.success)) return error.CompilerFailed;
    if (language == .cpp and builtin.os.tag == .macos) {
        // Verbose output consumes the same retained runtime metadata as linkers.
        // Every real link must still report both library paths after an update.
        const diagnostics = try dir.readFileAlloc(io, "compiler.log", arena, .limited(1024 * 1024));
        var lines = std.mem.splitScalar(u8, diagnostics, '\n');
        var links: usize = 0;
        while (lines.next()) |line| {
            if (!std.mem.startsWith(u8, line, "zig ld ")) continue;
            links += 1;
            try std.testing.expect(std.mem.find(u8, line, "libc++abi.a") != null);
            try std.testing.expect(std.mem.find(u8, line, "libc++.a") != null);
        }
        try std.testing.expect(links >= 3);
    }
}

fn fresh(arena: std.mem.Allocator, io: Io, command: []const []const u8, root: []const u8) !void {
    var child = try std.process.spawn(io, .{
        .argv = command,
        .cwd = .{ .path = root },
        .stdin = .pipe,
        .stdout = .pipe,
        .stderr = .inherit,
    });
    defer child.kill(io);
    var read_buffer: [4096]u8 = undefined;
    var write_buffer: [64]u8 = undefined;
    var reader = child.stdout.?.readerStreaming(io, &read_buffer);
    var writer = child.stdin.?.writerStreaming(io, &write_buffer);
    const client: std.zig.Client = .{ .in = &reader.interface, .out = &writer.interface };
    try run(arena, io, root, (try update(arena, client)).digest, 43);
    try client.serveBodylessMessage(.exit);
    if (!(try child.wait(io)).eql(.success)) return error.CompilerFailed;
}

const Result = struct { digest: std.Build.Cache.BinDigest, cache_hit: bool };

fn update(arena: std.mem.Allocator, client: std.zig.Client) !Result {
    try client.serveBodylessMessage(.update);
    var result: ?Result = null;
    while (true) {
        const header = try client.receiveMessage();
        const body = try arena.alloc(u8, header.bytes_len);
        defer arena.free(body);
        try client.in.readSliceAll(body);
        switch (header.tag) {
            .emit_digest => {
                if (body.len != 1 + std.Build.Cache.bin_digest_len) return error.InvalidDigest;
                var digest: std.Build.Cache.BinDigest = undefined;
                @memcpy(&digest, body[1..]);
                result = .{ .digest = digest, .cache_hit = body[0] & 1 != 0 };
            },
            .error_bundle => {
                if (body.len < 8) return error.InvalidDiagnostic;
                const extra_len = std.mem.readInt(u32, body[0..4], .little);
                if (extra_len != 0 and (body.len < 12 or std.mem.readInt(u32, body[8..12], .little) != 0))
                    return error.CompilationFailed;
                return result orelse error.NoArtifact;
            },
            else => {},
        }
    }
}

fn run(arena: std.mem.Allocator, io: Io, root: []const u8, digest: std.Build.Cache.BinDigest, expected: u8) !void {
    const executable = try std.fs.path.join(arena, &.{ root, "cache", "o", &std.Build.Cache.binToHex(digest), "program" });
    const result = try std.process.run(arena, io, .{ .argv = &.{executable}, .cwd = .{ .path = root } });
    defer arena.free(result.stdout);
    defer arena.free(result.stderr);
    if (result.term == .exited and result.term.exited == expected) return;
    std.debug.print("{s}: expected exit {d}, got {any}\n{s}\n", .{ executable, expected, result.term, result.stderr });
    return error.UnexpectedExitCode;
}
