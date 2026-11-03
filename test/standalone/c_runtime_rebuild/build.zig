const std = @import("std");

pub fn build(b: *std.Build) void {
    const test_step = b.step("test", "Test C runtime inputs across compiler updates");
    b.default_step = test_step;
    if (!std.process.can_spawn) return;
    switch (b.graph.host.result.os.tag) {
        .linux, .macos => {},
        else => return,
    }
    const driver = b.addExecutable(.{
        .name = "check-c-runtime-rebuild",
        .root_module = b.createModule(.{
            .root_source_file = b.path("main.zig"),
            .target = b.graph.host,
            .optimize = .debug,
        }),
        .use_llvm = true,
    });
    const run = b.addRunArtifact(driver);
    run.addFileArg(.zig_exe);
    run.addDirectoryArg(.zig_lib);
    run.addDirectoryArg(b.tmpPath());
    run.expectExitCode(0);
    run.has_side_effects = true;
    test_step.dependOn(&run.step);
}
