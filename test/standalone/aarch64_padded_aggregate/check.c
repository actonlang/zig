#include <stdint.h>

struct Padded { _Alignas(16) uint64_t value; };
struct Small { _Alignas(16) uint8_t value; };
_Static_assert(sizeof(struct Padded) == 16, "wrong layout");
_Static_assert(sizeof(struct Small) == 16, "wrong layout");

extern void copy_padded(struct Padded *, const struct Padded *);
extern void copy_small(struct Small *, const struct Small *);

int check(void) {
    struct Padded source = {42}, destination = {0};
    copy_padded(&destination, &source);
    if (destination.value != 42) return 1;
    struct Small small_source = {23}, small_destination = {0};
    copy_small(&small_destination, &small_source);
    if (small_destination.value != 23) return 2;
    return 0;
}
