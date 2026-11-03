#include <stdint.h>
#include "assert_or_panic.h"

extern void zig_conditional_initialize(void);
extern int zig_conditional_value(void), zig_conditional_return(int);
void c_check_conditional_return(void) {
    zig_conditional_initialize();
    assert_or_panic(zig_conditional_value() == 42);
    assert_or_panic(zig_conditional_return(0) == 42);
    assert_or_panic(zig_conditional_return(1) == 7);
}

struct ExternOffsetTable { int64_t first, second; unsigned char padding[4096]; int64_t last; };
struct ExternOffsetTable c_extern_offset_table = { .first=11, .second=23, .last=42 };
extern int64_t zig_extern_offset_first(void), zig_extern_offset_second(void), zig_extern_offset_last(void);
extern int64_t *zig_extern_offset_second_ptr(void), *zig_extern_offset_last_ptr(void);
extern void zig_extern_offset_set_second(int64_t value);
void c_check_extern_offsets(void) {
    assert_or_panic(zig_extern_offset_first() == 11);
    assert_or_panic(zig_extern_offset_second() == 23);
    assert_or_panic(zig_extern_offset_last() == 42);
    assert_or_panic(zig_extern_offset_second_ptr() == &c_extern_offset_table.second);
    assert_or_panic(zig_extern_offset_last_ptr() == &c_extern_offset_table.last);
    zig_extern_offset_set_second(31);
    assert_or_panic(c_extern_offset_table.second == 31);
    assert_or_panic(zig_extern_offset_second() == 31);
}

struct SpilledSretResult { uint64_t tag, first, second; };
extern struct SpilledSretResult zig_spilled_sret(void);
uint64_t c_sret_value(uint64_t value) {
#if defined(__aarch64__)
    __asm__ volatile("mov x8, #1" ::: "x8");
#endif
    return value;
}
void c_check_spilled_sret(void) {
    struct SpilledSretResult r = zig_spilled_sret();
    assert_or_panic(r.tag == 2);
    assert_or_panic(r.first == 276);
    assert_or_panic(r.second == 10);
}
