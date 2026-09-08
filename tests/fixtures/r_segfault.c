void mcp_test_segfault(void) {
  // Volatile preserves the invalid read when compiling with optimization.
  (void)*(volatile char *)1;
}
