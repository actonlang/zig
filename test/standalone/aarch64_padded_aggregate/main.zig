const Padded = extern struct { value: u64 align(16) };
const Small = extern struct { value: u8 align(16) };

comptime {
    if (@sizeOf(Padded) != 16 or @sizeOf(Small) != 16) @compileError("wrong layout");
}

extern "c" fn check() c_int;

pub export fn main() c_int {
    return check();
}

export fn copy_padded(dst: *Padded, src: *const Padded) void {
    dst.* = src.*;
}

export fn copy_small(dst: *Small, src: *const Small) void {
    dst.* = src.*;
}
