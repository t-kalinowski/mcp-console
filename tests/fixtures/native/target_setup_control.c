#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#else
#include <dlfcn.h>
static int (*native_poll)(struct pollfd *, nfds_t, int);
static ssize_t (*native_write)(int, const void *, size_t);
static ssize_t (*native_read)(int, void *, size_t);
#define poll native_poll
#define write native_write
#define read native_read
#endif

static const char *mode;
static int reached, release, cancelled, controlled;
static atomic_bool used;
static pthread_mutex_t prefix_lock = PTHREAD_MUTEX_INITIALIZER;
static int output_fd = -1;
static unsigned char prefix[32];
static size_t prefix_length;

static bool is_server(void) {
#ifdef __APPLE__
    return *_NSGetArgc() > 1 && strcmp((*_NSGetArgv())[1], "serve") == 0;
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) return false;
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    return next < (size_t)count && strcmp(args + next, "serve") == 0;
#endif
}

static int checkpoint(const char *root, const char *name) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(90);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) _exit(91);
    return fd;
}

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_poll = dlsym(RTLD_NEXT, "poll");
    native_write = dlsym(RTLD_NEXT, "write");
    native_read = dlsym(RTLD_NEXT, "read");
    if (!native_poll || !native_write || !native_read) _exit(90);
#endif
    const char *root = getenv("MCP_CONSOLE_TEST_TARGET_SETUP");
    if (!root || !is_server()) return;
    mode = getenv("MCP_CONSOLE_TEST_TARGET_FAULT");
    if (!mode) _exit(90);
    // Fault only the controller. Provider/helper processes keep their actual
    // observation, signal, protocol, and cleanup behavior.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    reached = checkpoint(root, "native-reached");
    release = checkpoint(root, "native-release");
    cancelled = checkpoint(root, "native-cancel");
    controlled = checkpoint(root, "native-controlled");
}

static void gate(int target_cancel) {
    if (write(reached, "1", 1) != 1) _exit(92);
    if (strcmp(mode, "poll-error-shutdown") == 0) {
        struct pollfd waits[] = {
            {.fd = target_cancel, .events = POLLIN},
            {.fd = cancelled, .events = POLLIN},
        };
        int result;
        do { result = poll(waits, 2, -1); } while (result < 0 && errno == EINTR);
        if (result <= 0) _exit(93);
        if (waits[1].revents) return;
        if (write(controlled, "1", 1) != 1) _exit(92);
    }
    struct pollfd waits[] = {
        {.fd = release, .events = POLLIN},
        {.fd = cancelled, .events = POLLIN},
    };
    int result;
    do { result = poll(waits, 2, -1); } while (result < 0 && errno == EINTR);
    if (result <= 0) _exit(93);
    if (waits[1].revents) return; // Persistent cancellation releases later gates.
    char byte;
    if (read(release, &byte, 1) != 1 || byte != '1') _exit(94);
}

static int observed_poll(struct pollfd *fds, nfds_t count, int timeout) {
    // The controller's command exit/cancel wait has two POLLIN descriptors and
    // its setup allowance. Observer/output and MCP waits have no such deadline.
    bool command_wait = mode && count == 2 && timeout >= 0 &&
        fds[0].events == POLLIN && fds[1].events == POLLIN;
    if (!command_wait || strcmp(mode, "input-error") == 0 || strcmp(mode, "read-error") == 0 || atomic_exchange(&used, true)) {
        return poll(fds, count, timeout);
    }
    if (strncmp(mode, "poll-error", 10) == 0) {
        gate(fds[1].fd);
        errno = EIO;
        return -1;
    }
    int result = poll(fds, count, timeout);
    if (result > 0) gate(fds[1].fd); // Freeze after exit/control receipt, before retirement.
    return result;
}

static ssize_t observed_write(int fd, const void *bytes, size_t count) {
    // OwnerInput is the length-prefixed captured session envelope. Match the
    // actual request write, not startup registration or an MCP response flush.
    if (mode && strcmp(mode, "input-error") == 0 && count > 15 &&
        memcmp((const char *)bytes + 4, "{\"session\":", 11) == 0 &&
        !atomic_exchange(&used, true)) {
        gate(-1);
        errno = EIO;
        return -1;
    }
    return write(fd, bytes, count);
}

static ssize_t observed_read(int fd, void *bytes, size_t count) {
    // Fragment reads deterministically so the failure retains the same partial
    // HELLO prefix regardless of how the pipe coalesces frame writes.
    if (mode && strcmp(mode, "read-error") == 0 && count > 1) count = 1;
    ssize_t result = read(fd, bytes, count);
    // The controller is reading the provider owner's HELLO, not JSONL MCP or
    // a private helper. A read failure denies that collector receipt evidence.
    bool inject = false;
    if (mode && strcmp(mode, "read-error") == 0 && result > 0) {
        pthread_mutex_lock(&prefix_lock);
        if (output_fd < 0 && *(const unsigned char *)bytes == 1) output_fd = fd;
        if (fd == output_fd && prefix_length < sizeof(prefix)) {
            size_t length = (size_t)result;
            if (length > sizeof(prefix) - prefix_length) length = sizeof(prefix) - prefix_length;
            memcpy(prefix + prefix_length, bytes, length);
            prefix_length += length;
            inject = prefix_length >= 16 && memcmp(prefix + 5, "{\"version\":", 11) == 0 && !atomic_exchange(&used, true);
        }
        pthread_mutex_unlock(&prefix_lock);
    }
    if (inject) {
        gate(-1);
        errno = EIO;
        return -1;
    }
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \
    interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&replacee };
INTERPOSE(observed_poll, poll)
INTERPOSE(observed_write, write)
INTERPOSE(observed_read, read)
#else
#undef poll
#undef write
#undef read
int poll(struct pollfd *fds, nfds_t count, int timeout) { return observed_poll(fds, count, timeout); }
ssize_t write(int fd, const void *bytes, size_t count) { return observed_write(fd, bytes, count); }
ssize_t read(int fd, void *bytes, size_t count) { return observed_read(fd, bytes, count); }
#endif
