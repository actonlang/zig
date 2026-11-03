#ifndef ZIG_C_ABI_ASSERT_OR_PANIC_H
#define ZIG_C_ABI_ASSERT_OR_PANIC_H

#include <stdbool.h>

void zig_panic(void);

static void assert_or_panic(bool ok) {
    if (!ok) {
        zig_panic();
    }
}

#endif
