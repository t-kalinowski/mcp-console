#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*real_read)(int, void *, size_t);
static ssize_t (*real_write)(int, const void *, size_t);
static int (*real_kill)(pid_t, int);
static int (*real_execvp)(const char *, char *const[]);
#else
#define real_read read
#define real_write write
#define real_kill kill
#define real_execvp execvp
#endif

static char role[32], destination[4096];
static int pending_fd = -1, interrupt_pending;
static unsigned char pending[4096];
static size_t pending_size;

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    real_read = dlsym(RTLD_NEXT, "read");
    real_write = dlsym(RTLD_NEXT, "write");
    real_kill = dlsym(RTLD_NEXT, "kill");
    real_execvp = dlsym(RTLD_NEXT, "execvp");
    if (!real_read || !real_write || !real_kill || !real_execvp) _exit(125);
#endif
    const char *value = getenv("MCP_CONSOLE_TEST_RESOLVER_FAULT");
    if (value) snprintf(role, sizeof(role), "%s", value);
    value = getenv("MCP_CONSOLE_TEST_RESOLVER_DESTINATION");
    if (value) snprintf(destination, sizeof(destination), "%s", value);
    /* The fixture belongs to the broker or workload, never its children. */
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t fault_write(int fd, const void *bytes, size_t count) {
    if (fd != 1 || count < 20 || !memmem(bytes, count, "\"version\":1", 11))
        return real_write(fd, bytes, count);
    if (strcmp(role, "diagnostic") == 0 && memmem(bytes, count, "\"embedding\"", 11)) {
        const char diagnostic[] = "resolver diagnostic: \xce\xb1\n";
        if (real_write(2, diagnostic, sizeof(diagnostic) - 1) < 0) _exit(125);
    }
    if (strcmp(role, "block") == 0 && memmem(bytes, count, "\"embedding\"", 11)) {
        int checkpoint = open(destination, O_WRONLY);
        if (checkpoint < 0 || real_write(checkpoint, "1", 1) != 1) _exit(125);
        close(checkpoint);
        for (;;) pause();
    }
    if (strcmp(role, "manifest") == 0 || strcmp(role, "r-manifest") == 0 || strcmp(role, "identity") == 0) {
        char *copy = malloc(count);
        if (!copy) _exit(125);
        memcpy(copy, bytes, count);
        const char *needle = strcmp(role, "manifest") == 0 ? "\"six>=1\"" :
            strcmp(role, "r-manifest") == 0 ? "\"requirements\":[\"cli\"]" : "\"python_home\":\"";
        char *found = memmem(copy, count, needle, strlen(needle));
        if (found) {
            if (strcmp(role, "manifest") == 0) memcpy(found + 1, "ten", 3);
            else if (strcmp(role, "r-manifest") == 0) found[17] = 'X';
            else found[strlen(needle)] = 'X';
        }
        ssize_t result = real_write(fd, copy, count);
        free(copy);
        return result;
    }
    return real_write(fd, bytes, count);
}

static ssize_t fault_read(int fd, void *buffer, size_t count) {
    if (fd == pending_fd) {
        if (interrupt_pending) {
            interrupt_pending = 0;
            errno = strcmp(role, "read-error") == 0 ? EIO : EINTR;
            return -1;
        }
        size_t length = count < pending_size ? count : pending_size;
        memcpy(buffer, pending, length);
        memmove(pending, pending + length, pending_size - length);
        pending_size -= length;
        if (!pending_size) pending_fd = -1;
        return (ssize_t)length;
    }
    ssize_t result = real_read(fd, buffer, count);
    if (result > 0 && (strcmp(role, "eintr") == 0 || strcmp(role, "read-error") == 0)) {
        unsigned char *found = memmem(buffer, (size_t)result, "resolver diagnostic: \xce", 22);
        if (found) {
            size_t prefix = (size_t)(found - (unsigned char *)buffer) + 22;
            pending_size = (size_t)result - prefix;
            if (!pending_size || pending_size > sizeof(pending)) _exit(125);
            memcpy(pending, (unsigned char *)buffer + prefix, pending_size);
            pending_fd = fd;
            interrupt_pending = 1;
            return (ssize_t)prefix;
        }
    }
    return result;
}

static int fault_kill(pid_t pid, int sig) {
    if (strcmp(role, "kill-error") == 0) {
        if (sig == SIGTERM) return 0;
        if (sig == SIGKILL) { errno = EPERM; return -1; }
    }
    return real_kill(pid, sig);
}

static int capture_execvp(const char *path, char *const args[]) {
    if (strcmp(role, "policy") == 0 && args[1] && args[2] &&
        strcmp(args[1], "--config-env") == 0 &&
        strcmp(args[2], "MCP_CONSOLE_RESOLVER_POLICY") == 0) {
        const char *configuration = getenv(args[2]);
        int output = open(destination, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (!configuration || output < 0 ||
            real_write(output, configuration, strlen(configuration)) < 0 ||
            real_write(output, "\n", 1) != 1) _exit(125);
        close(output);
    }
    return real_execvp(path, args);
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *a, *b; } interpose_##original \
    __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original };
INTERPOSE(fault_read, read)
INTERPOSE(fault_write, write)
INTERPOSE(fault_kill, kill)
INTERPOSE(capture_execvp, execvp)
#else
ssize_t read(int fd, void *buffer, size_t count) { return fault_read(fd, buffer, count); }
ssize_t write(int fd, const void *buffer, size_t count) { return fault_write(fd, buffer, count); }
int kill(pid_t pid, int sig) { return fault_kill(pid, sig); }
int execvp(const char *path, char *const args[]) { return capture_execvp(path, args); }
#endif
