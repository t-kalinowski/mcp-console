#define _GNU_SOURCE
#include <sys/mman.h>
#include <unistd.h>

void mcp_test_segfault(void) {
    void *page = mmap(NULL, (size_t)getpagesize(), PROT_READ,
                      MAP_PRIVATE | MAP_ANON, -1, 0);
    if (page == MAP_FAILED) _exit(90);
    // A real protection fault gives R the same signal cause on macOS and
    // Linux. kill(SIGSEGV) instead fills siginfo with a sender identity.
    *(volatile char *)page = 1;
}
