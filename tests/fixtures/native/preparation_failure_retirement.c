#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bool owner;
static atomic_bool captured;
static int controlled = -1;
static const char failure[] = "selected Python inspection failed (exit status: 23): captured setup failure\n";

__attribute__((constructor)) static void initialize(void) {
    const char *server = getenv("MCP_CONSOLE_TEST_FAILURE_SERVER");
    if (server == NULL) {
        char pid[32];
        snprintf(pid, sizeof(pid), "%ld", (long)getpid());
        setenv("MCP_CONSOLE_TEST_FAILURE_SERVER", pid, 1);
    } else {
        owner = getppid() == (pid_t)strtol(server, NULL, 10);
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static void notify(const char *name) {
    int fd = open(getenv(name), O_WRONLY | O_NONBLOCK);
    if (fd < 0 || write(fd, "1", 1) != 1) _exit(125);
    close(fd);
}

static void *capture_failure(size_t size) {
#ifdef __APPLE__
    void *result = malloc(size);
#else
    void *result = ((void *(*)(size_t))dlsym(RTLD_NEXT, "malloc"))(size);
#endif
    // Hold formatting of the real inspection failure after subprocess collection.
    // A later cancellation can be acknowledged while the failed operation still
    // owns host admission; it must not replace the captured terminal cause.
    if (owner && size == strlen(failure) && !atomic_exchange(&captured, true)) {
        notify("MCP_CONSOLE_TEST_FAILURE_CAPTURED");
        int fd = open(getenv("MCP_CONSOLE_TEST_FAILURE_RELEASE"), O_RDONLY);
        char token;
        ssize_t count;
        do { count = read(fd, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(125);
        close(fd);
    }
    return result;
}

static ssize_t observe_control(int fd, const void *buffer, size_t count) {
#ifdef __APPLE__
    ssize_t result = write(fd, buffer, count);
#else
    ssize_t result = ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(fd, buffer, count);
#endif
    if (owner && result == (ssize_t)count) {
        if (count > 14 && memcmp(buffer, "{\"Controlled\":", 14) == 0) controlled = fd;
        else if (fd == controlled && count == 1 && memcmp(buffer, "\n", 1) == 0) {
            controlled = -1;
            notify("MCP_CONSOLE_TEST_FAILURE_CONTROLLED");
        }
    }
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(capture_failure, malloc)
INTERPOSE(observe_control, write)
#else
void *malloc(size_t size) { return capture_failure(size); }
ssize_t write(int fd, const void *buffer, size_t count) { return observe_control(fd, buffer, count); }
#endif
