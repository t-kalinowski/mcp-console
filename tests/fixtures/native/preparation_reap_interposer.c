#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

struct joiner {
    pthread_t target;
    struct joiner *next;
};
static struct joiner *joiners;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static char *preparation_environment[5];

__attribute__((constructor)) static void initialize(void) {
    const char *names[] = {
#ifdef __APPLE__
        "DYLD_INSERT_LIBRARIES",
#else
        "LD_PRELOAD",
#endif
        "MCP_CONSOLE_TEST_REAP_PID", "MCP_CONSOLE_TEST_REAP_DONE",
        "MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE"
    };
    size_t count = 0;
    for (size_t index = 0; index < sizeof(names) / sizeof(names[0]); ++index) {
        const char *value = getenv(names[index]);
        if (value != NULL && asprintf(&preparation_environment[count++], "%s=%s", names[index], value) < 0)
            _exit(124);
    }
    // Production launches clear the broker environment. Keep this test hook
    // in the server and explicitly instrument only the broker at exec below.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static int instrument_preparation(const char *file, char *const arguments[]) {
    if (arguments[1] != NULL && strcmp(arguments[1], "resolve") == 0) {
        extern char **environ;
        char *environment[4096];
        size_t count = 0;
        for (char **entry = environ; *entry != NULL; ++entry) {
            if (count >= 4090) _exit(125);
            environment[count++] = *entry;
        }
        for (char **entry = preparation_environment; *entry != NULL; ++entry)
            environment[count++] = *entry;
        environment[count] = NULL;
        return execve(file, arguments, environment);
    }
#ifdef __APPLE__
    return execvp(file, arguments);
#else
    return ((int (*)(const char *, char *const []))dlsym(RTLD_NEXT, "execvp"))(file, arguments);
#endif
}

static ssize_t native_write(int fd, const void *bytes, size_t count) {
#ifdef __APPLE__
    return write(fd, bytes, count);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(fd, bytes, count);
#endif
}

static ssize_t observe_write(int fd, const void *bytes, size_t count) {
    if (count == 8 && memcmp(bytes, "\"Closed\"", 8) == 0) {
        int marker = open(getenv("MCP_CONSOLE_TEST_REAP_PID"), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        char pid[32];
        int length = snprintf(pid, sizeof(pid), "%ld", (long)getpid());
        if (marker < 0 || native_write(marker, pid, (size_t)length) != length) _exit(121);
        close(marker);
        const char *blocked = getenv("MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE");
        if (blocked != NULL) {
            int checkpoint = open(blocked, O_WRONLY);
            if (checkpoint < 0 || native_write(checkpoint, "1", 1) != 1) _exit(123);
            close(checkpoint);
            // The close request arrived, but this peer never acknowledges it
            // or exits. Only its controller can finish retirement.
            for (;;) pause();
        }
    }
    return native_write(fd, bytes, count);
}

static bool resolver(pid_t pid) {
    int marker = open(getenv("MCP_CONSOLE_TEST_REAP_PID"), O_RDONLY);
    if (marker < 0) return false;
    char text[32] = {0};
    ssize_t count = read(marker, text, sizeof(text) - 1);
    close(marker);
    return count > 0 && strtol(text, NULL, 10) == pid;
}

static bool joined(void) {
    for (struct joiner *entry = joiners; entry != NULL; entry = entry->next) {
        if (pthread_equal(entry->target, pthread_self())) return true;
    }
    return false;
}

static pid_t gated_waitpid(pid_t pid, int *status, int options) {
    bool held = pid > 0 && resolver(pid);
    if (held) {
        // Closed and EOF have reached the controller. Hold the actual reap
        // until its owner is joined, including a join that started earlier.
        pthread_mutex_lock(&lock);
        while (!joined()) pthread_cond_wait(&changed, &lock);
        pthread_mutex_unlock(&lock);
    }
#ifdef __APPLE__
    pid_t result = waitpid(pid, status, options);
#else
    pid_t result = ((pid_t (*)(pid_t, int *, int))dlsym(RTLD_NEXT, "waitpid"))(pid, status, options);
#endif
    int saved_errno = errno;
    if (held && result == pid) {
        int marker = open(getenv("MCP_CONSOLE_TEST_REAP_DONE"), O_WRONLY | O_CREAT, 0600);
        if (marker < 0) _exit(122);
        close(marker);
    }
    errno = saved_errno;
    return result;
}

static int observe_join(pthread_t thread, void **value) {
    struct joiner entry = { .target = thread };
    pthread_mutex_lock(&lock);
    entry.next = joiners;
    joiners = &entry;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
#ifdef __APPLE__
    int result = pthread_join(thread, value);
#else
    int result = ((int (*)(pthread_t, void **))dlsym(RTLD_NEXT, "pthread_join"))(thread, value);
#endif
    pthread_mutex_lock(&lock);
    struct joiner **current = &joiners;
    while (*current != &entry) current = &(*current)->next;
    *current = entry.next;
    pthread_mutex_unlock(&lock);
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)replacement, (const void *)original \
    };
INTERPOSE(observe_write, write)
INTERPOSE(gated_waitpid, waitpid)
INTERPOSE(observe_join, pthread_join)
INTERPOSE(instrument_preparation, execvp)
#else
ssize_t write(int fd, const void *bytes, size_t count) { return observe_write(fd, bytes, count); }
pid_t waitpid(pid_t pid, int *status, int options) { return gated_waitpid(pid, status, options); }
int pthread_join(pthread_t thread, void **value) { return observe_join(thread, value); }
int execvp(const char *file, char *const arguments[]) { return instrument_preparation(file, arguments); }
#endif
