#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_bool claimed = false;
static pid_t owner;
typedef ssize_t (*write_function)(int, const void *, size_t);

static write_function next_write(void) {
#ifdef __APPLE__
    return write;
#else
    return (write_function)dlsym(RTLD_NEXT, "write");
#endif
}

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t gated_write(int descriptor, const void *buffer, size_t count) {
    write_function write_next = next_write();
    const char *reached_path = getenv("MCP_CONSOLE_TEST_RESPONSE_WRITE_REACHED");
    const char *release_path = getenv("MCP_CONSOLE_TEST_RESPONSE_WRITE_RELEASE");
    const char match[] = "https://invalid.example/";
    bool matches = false;
    const unsigned char *bytes = buffer;
    for (size_t i = 0; i + sizeof(match) - 1 <= count; ++i) {
        if (memcmp(bytes + i, match, sizeof(match) - 1) == 0) { matches = true; break; }
    }
    if (descriptor != 1 || getpid() != owner || reached_path == NULL ||
        release_path == NULL || !matches || atomic_exchange(&claimed, true)) {
        return write_next(descriptor, buffer, count);
    }
    // Publish a real response prefix, then hold its remaining write behind a
    // FIFO. The checkpoint does not depend on output length or socket capacity.
    size_t prefix_size = count < 256 ? 1 : 256;
    ssize_t prefix = write_next(descriptor, buffer, prefix_size);
    if (prefix != (ssize_t)prefix_size) { _exit(125); }
    int release = open(release_path, O_RDONLY);
    int reached = open(reached_path, O_WRONLY);
    if (release < 0 || reached < 0 || write_next(reached, "1", 1) != 1) { _exit(125); }
    close(reached);
    char token;
    ssize_t received;
    do { received = read(release, &token, 1); } while (received < 0 && errno == EINTR);
    if (received != 1 || token != '1') { _exit(125); }
    close(release);
    return prefix;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&gated_write, (const void *)(uintptr_t)&write,
};
#else
ssize_t write(int descriptor, const void *buffer, size_t count) {
    return gated_write(descriptor, buffer, count);
}
#endif
