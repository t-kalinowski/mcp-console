#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

void mcp_test_segfault(void) {
  size_t size = (size_t)sysconf(_SC_PAGESIZE);
  void *address = mmap(NULL, size, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANON, -1, 0);
  if (address == MAP_FAILED || munmap(address, size) != 0) {
    abort();
  }

  // Write to the now-unmapped page; volatile preserves the faulting instruction.
  *(volatile unsigned char *)address = 0;
}
