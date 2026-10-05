#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Hold the initial error and then the first surviving send's response. Keeping
// both gates in one stdout writer avoids assumptions about socket capacity.
static atomic_uint next_gate = 0;
static pid_t owner;
typedef ssize_t (*write_function)(int, const void *, size_t);

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t gated_write(int descriptor, const void *buffer, size_t count) {
#ifdef __APPLE__
    write_function write_next = write;
#else
    write_function write_next = (write_function)dlsym(RTLD_NEXT, "write");
#endif
    unsigned int gate = atomic_load(&next_gate);
    if (descriptor != 1 || getpid() != owner || gate >= 2)
        return write_next(descriptor, buffer, count);
    const char *matches[] = {"https://invalid.example/", "zod response-gated operation"};
    const char *reached_names[] = {
        "MCP_CONSOLE_TEST_INITIAL_WRITE_REACHED", "MCP_CONSOLE_TEST_LIVE_WRITE_REACHED"
    };
    const char *release_names[] = {
        "MCP_CONSOLE_TEST_INITIAL_WRITE_RELEASE", "MCP_CONSOLE_TEST_LIVE_WRITE_RELEASE"
    };
    size_t match_size = strlen(matches[gate]);
    const unsigned char *bytes = buffer;
    size_t i;
    for (i = 0; i + match_size <= count; ++i) {
        if (memcmp(bytes + i, matches[gate], match_size) == 0) break;
    }
    if (i + match_size > count || !atomic_compare_exchange_strong(&next_gate, &gate, gate + 1))
        return write_next(descriptor, buffer, count);

    // Keep at least the trailing newline unwritten even for a small live result.
    size_t prefix_size = count > 256 ? 256 : count - 1;
    ssize_t prefix = write_next(descriptor, buffer, prefix_size);
    if (prefix != (ssize_t)prefix_size) _exit(125);
    int release = open(getenv(release_names[gate]), O_RDONLY);
    int reached = open(getenv(reached_names[gate]), O_WRONLY);
    if (release < 0 || reached < 0 || write_next(reached, "1", 1) != 1) _exit(125);
    close(reached);
    char token;
    ssize_t received;
    do { received = read(release, &token, 1); } while (received < 0 && errno == EINTR);
    if (received != 1 || token != '1') _exit(125);
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
