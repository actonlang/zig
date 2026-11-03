const std = @import("std");

pub fn build(b: *std.Build) void {
    const test_step = b.step("test", "Test native AArch64 slice field addresses");
    b.default_step = test_step;
    if (!std.process.can_spawn or b.graph.host.result.cpu.arch != .aarch64) return;

    const exe = b.addExecutable(.{
        .name = "slice-field",
        .root_module = b.createModule(.{
            .root_source_file = b.path("main.zig"),
            .target = b.graph.host,
            .optimize = .debug,
            .link_libc = true,
        }),
        .use_llvm = false,
    });
    exe.use_new_linker = false;
    const run = b.addRunArtifact(exe);
    run.expectExitCode(0);
    test_step.dependOn(&run.step);
}
