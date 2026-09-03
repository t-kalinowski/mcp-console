#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_bool claimed = false;
static atomic_uintptr_t blocked_thread = 0;
#define TRACKED_DESCRIPTORS 1024
static atomic_size_t match_lengths[TRACKED_DESCRIPTORS];

typedef ssize_t (*read_function)(int, void *, size_t);
typedef int (*pthread_join_function)(pthread_t, void **);

static read_function next_read(void) {
    return read;
}

static pthread_join_function next_pthread_join(void) {
    return pthread_join;
}

static bool target_process(void) {
    const char *value = getenv("MCP_CONSOLE_TEST_RELAY_READ_PID");
    if (value == NULL) {
        return false;
    }
    char *end = NULL;
    long process_id = strtol(value, &end, 10);
    return end != value && *end == '\0' && process_id == getpid();
}

static bool contains(int descriptor, const void *buffer, size_t length,
                     const char *match) {
    size_t match_length = strlen(match);
    if (match_length == 0) {
        return false;
    }
    size_t matched = 0;
    if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
        matched = atomic_load(&match_lengths[descriptor]);
    }
    const unsigned char *bytes = buffer;
    for (size_t index = 0; index < length; ++index) {
        if (bytes[index] == (unsigned char)match[matched]) {
            ++matched;
        } else {
            matched = bytes[index] == (unsigned char)match[0] ? 1 : 0;
        }
        if (matched == match_length) {
            if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
                atomic_store(&match_lengths[descriptor], 0);
            }
            return true;
        }
    }
    if (descriptor >= 0 && descriptor < TRACKED_DESCRIPTORS) {
        atomic_store(&match_lengths[descriptor], matched);
    }
    return false;
}

static void notify(const char *variable) {
    const char *path = getenv(variable);
    if (path == NULL) {
        return;
    }
    int descriptor;
    do {
        descriptor = open(path, O_WRONLY | O_NONBLOCK);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        return;
    }
    (void)write(descriptor, "1", 1);
    close(descriptor);
}

static void release_blocked_read(void) {
    notify("MCP_CONSOLE_TEST_RELAY_READ_RELEASE");
}

static ssize_t delayed_read(int descriptor, void *buffer, size_t length) {
    read_function read_next = next_read();
    ssize_t result = read_next(descriptor, buffer, length);
    const char *match = getenv("MCP_CONSOLE_TEST_RELAY_READ_MATCH");
    if (result <= 0 || !target_process() || match == NULL ||
        !contains(descriptor, buffer, (size_t)result, match) ||
        atomic_exchange(&claimed, true)) {
        return result;
    }

    atomic_store(&blocked_thread, (uintptr_t)pthread_self());
    notify("MCP_CONSOLE_TEST_RELAY_READ_BLOCKED");
    const char *release = getenv("MCP_CONSOLE_TEST_RELAY_READ_RELEASE");
    if (release == NULL) {
        return result;
    }
    int release_descriptor;
    do {
        release_descriptor = open(release, O_RDONLY);
    } while (release_descriptor < 0 && errno == EINTR);
    if (release_descriptor < 0) {
        return result;
    }
    char token;
    while (read_next(release_descriptor, &token, 1) < 0 && errno == EINTR) {
    }
    close(release_descriptor);
    return result;
}

static int releasing_pthread_join(pthread_t thread, void **result) {
    pthread_join_function join_next = next_pthread_join();
    uintptr_t blocked = atomic_load(&blocked_thread);
    if (blocked != 0 && pthread_equal(thread, (pthread_t)blocked)) {
        release_blocked_read();
    }
    return join_next(thread, result);
}

__attribute__((constructor)) static void prevent_child_injection(void) {
    if (target_process()) {
        unsetenv("DYLD_INSERT_LIBRARIES");
    }
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(delayed_read, read)
DYLD_INTERPOSE(releasing_pthread_join, pthread_join)
