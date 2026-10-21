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

static pid_t owner;
static atomic_bool claimed;
typedef ssize_t (*read_function)(int, void *, size_t);

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t gated_read(int descriptor, void *buffer, size_t length) {
#ifdef __APPLE__
    read_function read_next = read;
#else
    read_function read_next = (read_function)dlsym(RTLD_NEXT, "read");
#endif
    ssize_t result = read_next(descriptor, buffer, length);
    const char *marker = getenv("MCP_CONSOLE_TEST_EXPORT_SCAN_MARKER");
    if (marker == NULL) marker = "{\"event\":\"recording_started\",";
    size_t marker_length = strlen(marker);
    const char *armed = getenv("MCP_CONSOLE_TEST_EXPORT_SCAN_ARMED");
    if (getpid() != owner || armed == NULL || access(armed, F_OK) != 0 ||
        result < (ssize_t)marker_length ||
        memcmp(buffer, marker, marker_length) != 0 ||
        atomic_exchange(&claimed, true)) return result;

    int reached = open(getenv("MCP_CONSOLE_TEST_EXPORT_SCAN_REACHED"), O_WRONLY);
    int release = open(getenv("MCP_CONSOLE_TEST_EXPORT_SCAN_RELEASE"), O_RDONLY);
    if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(120);
    char token;
    ssize_t count;
    do { count = read_next(release, &token, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(121);
    close(reached);
    close(release);
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} interpose_read __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&gated_read, (const void *)(uintptr_t)&read,
};
#else
ssize_t read(int descriptor, void *buffer, size_t length) {
    return gated_read(descriptor, buffer, length);
}
#endif
