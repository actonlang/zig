const std = @import("std");
const builtin = @import("builtin");
const Io = std.Io;

const Mode = enum { whole, incremental };
const Case = enum { optional, shadow, missing_directory, directory_header, include_next, presence_only, blocked_parent, symlink_parent };

pub fn main(init: std.process.Init) !void {
    const arena = init.arena.allocator();
    const args = try init.minimal.args.toSlice(arena);
    if (args.len != 4) return error.BadArguments;
    const zig = try Io.Dir.cwd().realPathFileAlloc(init.io, args[1], arena);
    const zig_lib = try Io.Dir.cwd().realPathFileAlloc(init.io, args[2], arena);
    const scratch = try Io.Dir.cwd().realPathFileAlloc(init.io, args[3], arena);
    const global_cache = try std.fs.path.join(arena, &.{ scratch, "global-cache" });
    for (std.enums.values(Mode)) |mode| {
        for (std.enums.values(Case)) |case| {
            // This case checks POSIX traversal through a symlink followed by '..'.
            if (case == .symlink_parent and builtin.os.tag == .windows) continue;
            const root = try std.fs.path.join(arena, &.{ scratch, @tagName(mode), @tagName(case) });
            const dir = try Io.Dir.cwd().createDirPathOpen(init.io, root, .{});
            defer dir.close(init.io);
            const flags: []const []const u8 = switch (case) {
                .shadow => &.{ "-I", "first", "-I", "fallback" },
                .missing_directory => &.{ "-I", "missing/deep", "-I", "fallback" },
                .include_next => &.{ "-I", "first", "-I", "middle", "-I", "last" },
                .symlink_parent => &.{ "-I", "include" },
                else => &.{},
            };
            const initial: u8 = switch (case) {
                .optional, .directory_header, .blocked_parent => 0,
                .presence_only => 23,
                else => 11,
            };
            const changed: u8 = if (case == .presence_only) 0 else 23;
            const body = switch (case) {
                .optional, .directory_header =>
                \\#if __has_include("optional.h")
                \\#include "optional.h"
                \\#else
                \\#define VALUE 0
                \\#endif
                ,
                .presence_only =>
                \\#if __has_include("present.h")
                \\#define VALUE 23
                \\#else
                \\#define VALUE 0
                \\#endif
                ,
                .blocked_parent =>
                \\#if __has_include("blocked/deep/value.h")
                \\#include "blocked/deep/value.h"
                \\#else
                \\#define VALUE 0
                \\#endif
                ,
                .include_next => "#include <chain.h>",
                .symlink_parent => "#include <link/../value.h>",
                else => "#include <value.h>",
            };
            try write(dir, init.io, "main.c", try std.fmt.allocPrint(arena, "{s}\nint main(void) {{ return VALUE; }}\n", .{body}));
            switch (case) {
                .optional => {},
                .directory_header => try write(dir, init.io, "optional.h/child", "not a header"),
                .shadow, .missing_directory => {
                    if (case == .shadow) try dir.createDir(init.io, "first", .default_dir);
                    try write(dir, init.io, "fallback/value.h", "#define VALUE 11\n");
                },
                .include_next => {
                    try write(dir, init.io, "first/chain.h", "#include_next <chain.h>\n");
                    try dir.createDir(init.io, "middle", .default_dir);
                    try write(dir, init.io, "last/chain.h", "#define VALUE 11\n");
                },
                .presence_only => try write(dir, init.io, "present.h", ""),
                .blocked_parent => try write(dir, init.io, "blocked", "not a directory"),
                .symlink_parent => {
                    try dir.createDirPath(init.io, "target/nested");
                    try write(dir, init.io, "target/value.h", "#define VALUE 11\n");
                    try write(dir, init.io, "include/value.h", "#define VALUE 77\n");
                    try dir.symLink(init.io, "../target/nested", "include/link", .{ .is_directory = true });
                },
            }
            var command: std.ArrayList([]const u8) = .empty;
            try command.appendSlice(arena, &.{ zig, "build-exe", "main.c", "-lc", "-fno-sanitize-c", "--zig-lib-dir", zig_lib, "--cache-dir", "cache", "--global-cache-dir", global_cache });
            try command.append(arena, if (mode == .whole) "-fno-incremental" else "-fincremental");
            if (builtin.os.tag.isDarwin()) try command.append(arena, "-fno-new-linker");
            const output_name = if (builtin.os.tag == .windows) "program.exe" else "program";
            try command.appendSlice(arena, &.{ "--name", "program", "--listen=-" });
            try command.appendSlice(arena, flags);
            _ = try compileAndRun(arena, init.io, command.items, root, output_name, initial, case, mode);
            const cache_hit = try compileAndRun(arena, init.io, command.items, root, output_name, initial, case, mode);
            if (mode == .whole and !cache_hit) return error.ExpectedCacheHit;
            switch (case) {
                .optional => try write(dir, init.io, "optional.h", "#define VALUE 23\n"),
                .directory_header => {
                    try dir.deleteTree(init.io, "optional.h");
                    try write(dir, init.io, "optional.h", "#define VALUE 23\n");
                },
                .shadow => try write(dir, init.io, "first/value.h", "#define VALUE 23\n"),
                .missing_directory => try write(dir, init.io, "missing/deep/value.h", "#define VALUE 23\n"),
                .include_next => try write(dir, init.io, "middle/chain.h", "#define VALUE 23\n"),
                .presence_only => try dir.deleteFile(init.io, "present.h"),
                .blocked_parent => {
                    try dir.deleteFile(init.io, "blocked");
                    try write(dir, init.io, "blocked/deep/value.h", "#define VALUE 23\n");
                },
                .symlink_parent => try write(dir, init.io, "target/value.h", "#define VALUE 23\n/* changed target, unchanged lexical sentinel */\n"),
            }
            _ = try compileAndRun(arena, init.io, command.items, root, output_name, changed, case, mode);
        }
    }
    try testCollectorFailure(arena, init.io, zig, zig_lib, scratch, global_cache);
}

fn testCollectorFailure(arena: std.mem.Allocator, io: Io, zig: []const u8, zig_lib: []const u8, scratch: []const u8, global_cache: []const u8) !void {
    const root = try std.fs.path.join(arena, &.{ scratch, "collector-failure" });
    const dir = try Io.Dir.cwd().createDirPathOpen(io, root, .{});
    defer dir.close(io);
    try write(dir, io, "main.c", "int main(void) { return 0; }\n");
    const output_name = if (builtin.os.tag == .windows) "program.exe" else "program";
    var common: std.ArrayList([]const u8) = .empty;
    try common.appendSlice(arena, &.{ zig, "build-exe", "-lc", "-fno-sanitize-c", "--zig-lib-dir", zig_lib, "--cache-dir", "cache", "--global-cache-dir", global_cache });
    if (builtin.os.tag.isDarwin()) try common.append(arena, "-fno-new-linker");
    try common.append(arena, try std.fmt.allocPrint(arena, "-femit-bin={s}", .{output_name}));
    var failing: std.ArrayList([]const u8) = .empty;
    try failing.appendSlice(arena, common.items);
    // Override the collector's destination after its internal option. Clang
    // successfully compiles the source before this write fails, leaving no C
    // diagnostic to deserialize. Zig must report the collector's stderr.
    try failing.appendSlice(arena, &.{ "-cflags", "-Xclang", "-zig-cache-dependencies", "-Xclang", "missing/output.inputs", "--", "main.c" });
    const result = try std.process.run(arena, io, .{ .argv = failing.items, .cwd = .{ .path = root } });
    defer arena.free(result.stdout);
    defer arena.free(result.stderr);
    const failed_cleanly = switch (result.term) {
        .exited => |code| code != 0,
        else => false,
    };
    if (!failed_cleanly or std.mem.indexOf(u8, result.stderr, "cannot write Zig cache dependencies") == null) {
        std.debug.print("collector failure: expected an error diagnostic, got {any}\n{s}\n", .{ result.term, result.stderr });
        return error.ExpectedCollectorDiagnostic;
    }
    try common.append(arena, "main.c");
    try run(arena, io, common.items, root, 0, "collector recovery compile");
    const executable = try std.fs.path.join(arena, &.{ root, output_name });
    try run(arena, io, &.{executable}, root, 0, "collector recovery execution");
}

fn write(dir: Io.Dir, io: Io, path: []const u8, contents: []const u8) !void {
    if (std.fs.path.dirname(path)) |parent| try dir.createDirPath(io, parent);
    try dir.writeFile(io, .{ .sub_path = path, .data = contents });
}

fn compileAndRun(arena: std.mem.Allocator, io: Io, command: []const []const u8, root: []const u8, output_name: []const u8, expected: u8, case: Case, mode: Mode) !bool {
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
    try client.serveBodylessMessage(.update);
    var digest: ?std.Build.Cache.BinDigest = null;
    var cache_hit = false;
    while (true) {
        const header = try client.receiveMessage();
        const body = try arena.alloc(u8, header.bytes_len);
        defer arena.free(body);
        try reader.interface.readSliceAll(body);
        switch (header.tag) {
            .emit_digest => {
                if (body.len != 1 + std.Build.Cache.bin_digest_len) return error.InvalidDigest;
                cache_hit = @as(std.zig.Server.Message.EmitDigest.Flags, @bitCast(body[0])).cache_hit;
                var bytes: std.Build.Cache.BinDigest = undefined;
                @memcpy(&bytes, body[1..]);
                digest = bytes;
            },
            .error_bundle => {
                if (body.len < 8) return error.InvalidDiagnostic;
                const extra_len = std.mem.readInt(u32, body[0..4], .little);
                if (extra_len != 0 and (body.len < 12 or std.mem.readInt(u32, body[8..12], .little) != 0))
                    return error.CompilationFailed;
                break;
            },
            else => {},
        }
    }
    try client.serveBodylessMessage(.exit);
    if (!(try child.wait(io)).eql(.success)) return error.CompilerFailed;
    const hex = std.Build.Cache.binToHex(digest orelse return error.NoArtifact);
    const executable = try std.fs.path.join(arena, &.{ root, "cache", "o", &hex, output_name });
    try run(arena, io, &.{executable}, root, expected, try std.fmt.allocPrint(arena, "{s}/{s}", .{ @tagName(mode), @tagName(case) }));
    return cache_hit;
}

fn run(arena: std.mem.Allocator, io: Io, command: []const []const u8, cwd: []const u8, expected: u8, context: []const u8) !void {
    const result = try std.process.run(arena, io, .{ .argv = command, .cwd = .{ .path = cwd } });
    defer arena.free(result.stdout);
    defer arena.free(result.stderr);
    switch (result.term) {
        .exited => |code| if (code == expected) return,
        else => {},
    }
    std.debug.print("{s}: {s}: expected exit {d}, got {any}\n{s}\n", .{ context, command[0], expected, result.term, result.stderr });
    return error.UnexpectedExitCode;
}
