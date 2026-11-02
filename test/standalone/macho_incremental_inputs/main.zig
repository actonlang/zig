const std = @import("std");
const Io = std.Io;

pub fn main(init: std.process.Init) !void {
    const arena = init.arena.allocator();
    const io = init.io;
    const args = try init.minimal.args.toSlice(arena);
    if (args.len != 4) return error.BadArguments;
    const zig = try Io.Dir.cwd().realPathFileAlloc(io, args[1], arena);
    const zig_lib = try Io.Dir.cwd().realPathFileAlloc(io, args[2], arena);
    const root = try Io.Dir.cwd().realPathFileAlloc(io, args[3], arena);
    const dir = try Io.Dir.cwd().openDir(io, root, .{});
    defer dir.close(io);
    try dir.writeFile(io, .{ .sub_path = "support.c", .data = "extern int value(void); int main(void) { return value(); }\n" });
    try run(arena, io, &.{ zig, "build-obj", "support.c", "-fno-sanitize-c", "--zig-lib-dir", zig_lib, "--cache-dir", "cache", "--global-cache-dir", "global", "-femit-bin=support.o" }, root, 0);
    try dir.writeFile(io, .{ .sub_path = "value.zig", .data = "pub extern fn main() callconv(.c) c_int; export fn value() callconv(.c) c_int { return 42; }\n" });
    var child = try std.process.spawn(io, .{
        .argv = &.{ zig, "build-exe", "value.zig", "support.o", "-lc", "-fno-llvm", "-fno-new-linker", "-fno-compiler-rt", "-fincremental", "--zig-lib-dir", zig_lib, "--cache-dir", "cache", "--global-cache-dir", "global", "--name", "value", "--listen=-" },
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
    for ([_]u8{ 42, 43, 52, 44 }) |expected| {
        try dir.writeFile(io, .{ .sub_path = "value.zig", .data = try std.fmt.allocPrint(arena,
            \\pub extern fn main() callconv(.c) c_int;
            \\export fn value() callconv(.c) c_int {{ return {d}; }}
            \\
        , .{expected}) });
        const digest = try update(arena, client);
        const executable = try std.fs.path.join(arena, &.{ root, "cache", "o", &std.Build.Cache.binToHex(digest), "value" });
        try run(arena, io, &.{executable}, root, expected);
    }
    try client.serveBodylessMessage(.exit);
    if (!(try child.wait(io)).success()) return error.CompilerFailed;
}

fn update(arena: std.mem.Allocator, client: std.zig.Client) !std.Build.Cache.BinDigest {
    try client.serveBodylessMessage(.update);
    var digest: ?std.Build.Cache.BinDigest = null;
    while (true) {
        const header = try client.receiveMessage();
        const body = try arena.alloc(u8, header.bytes_len);
        defer arena.free(body);
        try client.in.readSliceAll(body);
        switch (header.tag) {
            .emit_digest => {
                if (body.len != 1 + std.Build.Cache.bin_digest_len) return error.InvalidDigest;
                var bytes: std.Build.Cache.BinDigest = undefined;
                @memcpy(&bytes, body[1..]);
                digest = bytes;
            },
            .error_bundle => {
                if (body.len < 8) return error.InvalidDiagnostic;
                const extra_len = std.mem.readInt(u32, body[0..4], .little);
                if (extra_len != 0 and (body.len < 12 or std.mem.readInt(u32, body[8..12], .little) != 0))
                    return error.CompilationFailed;
                return digest orelse error.NoArtifact;
            },
            else => {},
        }
    }
}

fn run(arena: std.mem.Allocator, io: Io, command: []const []const u8, cwd: []const u8, expected: u8) !void {
    const result = try std.process.run(arena, io, .{ .argv = command, .cwd = .{ .path = cwd } });
    defer arena.free(result.stdout);
    defer arena.free(result.stderr);
    switch (result.term) {
        .exited => |code| if (code == expected) return,
        else => {},
    }
    std.debug.print("{s}: expected exit {d}, got {any}\n{s}\n", .{ command[0], expected, result.term, result.stderr });
    return error.UnexpectedExitCode;
}
