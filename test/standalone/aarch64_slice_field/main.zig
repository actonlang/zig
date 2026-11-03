var bytes = [_]u8{ 1, 2, 3 };
var mutable: []u8 = &bytes;
var nested: struct { data: []u8 } = .{ .data = &bytes };

pub export fn main() c_int {
    const ptr_ptr = &mutable.ptr;
    const len_ptr = &mutable.len;
    if (ptr_ptr.* != &bytes or len_ptr.* != 3) return 1;
    if (@intFromPtr(len_ptr) != @intFromPtr(ptr_ptr) +% @sizeOf(usize)) return 2;
    if (nested.data.len != 3 or nested.data.ptr != &bytes) return 3;
    len_ptr.* = 2;
    if (mutable.len != 2 or mutable.ptr != &bytes) return 4;
    return 0;
}
