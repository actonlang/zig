export fn zig_panic() noreturn {
    @panic("zig_panic called from C");
}

var conditional_done: bool = false;
var conditional_value: c_int = 0;
export fn zig_conditional_initialize() void {
    if (conditional_done) return;
    conditional_done = true;
    conditional_value = 42;
}
export fn zig_conditional_value() c_int {
    return conditional_value;
}
export fn zig_conditional_return(n: c_int) c_int {
    if (n != 0) return 7;
    return 42;
}
extern fn c_check_conditional_return() void;
test "conditional early return through C" {
    c_check_conditional_return();
}

const ExternOffsetTable = extern struct { first: i64, second: i64, padding: [4096]u8, last: i64 };
extern var c_extern_offset_table: ExternOffsetTable;
export fn zig_extern_offset_first() callconv(.c) i64 {
    return c_extern_offset_table.first;
}
export fn zig_extern_offset_second() callconv(.c) i64 {
    return c_extern_offset_table.second;
}
export fn zig_extern_offset_last() callconv(.c) i64 {
    return c_extern_offset_table.last;
}
export fn zig_extern_offset_second_ptr() callconv(.c) *i64 {
    return &c_extern_offset_table.second;
}
export fn zig_extern_offset_last_ptr() callconv(.c) *i64 {
    return &c_extern_offset_table.last;
}
export fn zig_extern_offset_set_second(value: i64) callconv(.c) void {
    c_extern_offset_table.second = value;
}
extern fn c_check_extern_offsets() void;
test "external global offsets through C" {
    c_check_extern_offsets();
}

const SpilledSretResult = extern struct { tag: u64, first: u64, second: u64 };
extern fn c_sret_value(value: u64) callconv(.c) u64;
export fn zig_spilled_sret() callconv(.c) SpilledSretResult {
    const v0 = c_sret_value(0);
    const v1 = c_sret_value(1);
    const v2 = c_sret_value(2);
    const v3 = c_sret_value(3);
    const v4 = c_sret_value(4);
    const v5 = c_sret_value(5);
    const v6 = c_sret_value(6);
    const v7 = c_sret_value(7);
    const v8 = c_sret_value(8);
    const v9 = c_sret_value(9);
    const v10 = c_sret_value(10);
    const v11 = c_sret_value(11);
    const v12 = c_sret_value(12);
    const v13 = c_sret_value(13);
    const v14 = c_sret_value(14);
    const v15 = c_sret_value(15);
    const v16 = c_sret_value(16);
    const v17 = c_sret_value(17);
    const v18 = c_sret_value(18);
    const v19 = c_sret_value(19);
    const v20 = c_sret_value(20);
    const v21 = c_sret_value(21);
    const v22 = c_sret_value(22);
    const v23 = c_sret_value(23);
    return .{ .tag = 2, .first = v0 +% v1 +% v2 +% v3 +% v4 +% v5 +% v6 +% v7 +% v8 +% v9 +% v10 +% v11 +% v12 +% v13 +% v14 +% v15 +% v16 +% v17 +% v18 +% v19 +% v20 +% v21 +% v22 +% v23, .second = 10 };
}
extern fn c_check_spilled_sret() void;
test "indirect result pointer under register pressure" {
    c_check_spilled_sret();
}
