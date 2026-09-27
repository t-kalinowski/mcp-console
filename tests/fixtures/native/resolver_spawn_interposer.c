#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_uint fork_count = 0;
static pid_t owner;
static int gated;

static int is_server(void) {
    return owner == getpid();
}

__attribute__((constructor)) static void prevent_child_injection(void) {
    const char *server = getenv("MCP_CONSOLE_TEST_SPAWN_SERVER");
    if ((server != NULL && strtol(server, NULL, 10) == getpid()) ||
        getenv("MCP_CONSOLE_TEST_SPAWN_WORKLOAD") != NULL) {
        owner = getpid();
    }
    if (is_server()) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static pid_t checkpoint_fork(void) {
    const char *armed = getenv("MCP_CONSOLE_TEST_SPAWN_ARMED");
    const char *ordinal = getenv("MCP_CONSOLE_TEST_SPAWN_ORDINAL");
    if (is_server() && armed != NULL && access(armed, F_OK) == 0 &&
        atomic_fetch_add(&fork_count, 1) + 1 == strtoul(ordinal, NULL, 10) &&
        unlink(armed) == 0) {
        int started = open(getenv("MCP_CONSOLE_TEST_SPAWN_STARTED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_SPAWN_RELEASE"), O_RDONLY);
        if (started < 0 || release < 0 || write(started, "1", 1) != 1) {
            _exit(121);
        }
        char token;
        ssize_t result;
        do {
            result = read(release, &token, 1);
        } while (result < 0 && errno == EINTR);
        if (result != 1 || token != '1') {
            _exit(122);
        }
        close(started);
        close(release);
        gated = 1;
    }
#ifdef __APPLE__
    return fork();
#else
    return ((pid_t (*)(void))dlsym(RTLD_NEXT, "fork"))();
#endif
}

static int checkpoint_execvp(const char *path, char *const arguments[]) {
    const char *executed = getenv("MCP_CONSOLE_TEST_SPAWN_EXECUTED");
    if (gated && executed != NULL) {
        int descriptor = open(executed, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (descriptor < 0 || write(descriptor, path, strlen(path)) < 0) {
            _exit(123);
        }
        close(descriptor);
    }
#ifdef __APPLE__
    return execvp(path, arguments);
#else
    return ((int (*)(const char *, char *const[]))dlsym(RTLD_NEXT, "execvp"))(path, arguments);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} fork_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&checkpoint_fork,
    (const void *)(uintptr_t)&fork,
};
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} execvp_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&checkpoint_execvp,
    (const void *)(uintptr_t)&execvp,
};

#else
pid_t fork(void) { return checkpoint_fork(); }
int execvp(const char *path, char *const arguments[]) {
    return checkpoint_execvp(path, arguments);
}
#endif
