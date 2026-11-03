const builtin = @import("builtin");
extern "c" fn printf([*:0]const u8, ...) c_int;
pub export fn main() c_int {
    if (builtin.test_functions.len != 3) return 1;
    for (builtin.test_functions) |case| {
        case.func() catch return 2;
        if (printf("PASS %.*s\n", @as(c_int, @intCast(case.name.len)), case.name.ptr) < 0) return 3;
    }
    return 0;
}
