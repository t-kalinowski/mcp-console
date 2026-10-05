#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#else
#include <dlfcn.h>
static int (*native_lstat)(const char *, struct stat *);
static int (*native_statx)(int, const char *, int, unsigned int, struct statx *);
static ssize_t (*native_read)(int, void *, size_t);
static ssize_t (*native_write)(int, const void *, size_t);
#define lstat native_lstat
#define read native_read
#define write native_write
#endif

static const char *selected;
static atomic_bool used;
static atomic_bool eof;
static bool lose_close_receipt;

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_lstat = dlsym(RTLD_NEXT, "lstat");
    native_statx = dlsym(RTLD_NEXT, "statx");
    native_read = dlsym(RTLD_NEXT, "read");
    native_write = dlsym(RTLD_NEXT, "write");
    if (!native_lstat || !native_statx || !native_read || !native_write) _exit(120);
#endif
    const char *path = getenv("MCP_CONSOLE_TEST_STARTUP_SELECTION");
    if (!path) return;
#ifdef __APPLE__
    if (*_NSGetArgc() < 2) return;
    const char *command = (*_NSGetArgv())[1];
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) _exit(121);
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) _exit(121);
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    if (next >= (size_t)count) return;
    const char *command = args + next;
#endif
    bool drop_receipt = getenv("MCP_CONSOLE_TEST_STARTUP_LOSE_CLOSE") != NULL;
    if (strcmp(command, "resolve") == 0) lose_close_receipt = drop_receipt;
    else if (strcmp(command, "serve") == 0) selected = path;
    else return;
    // Discovery and inspection children retain real process/I/O behavior.
    if (!selected || !drop_receipt) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static ssize_t observed_write(int fd, const void *buffer, size_t count) {
    // Only the preparation child loses its final close receipt, after its
    // operation result confirmed native cleanup. CLI exit cannot replace it.
    if (lose_close_receipt && count == 8 && memcmp(buffer, "\"Closed\"", 8) == 0) {
        int marker = open(getenv("MCP_CONSOLE_TEST_STARTUP_CLOSE_LOST"),
                          O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
        if (marker < 0) _exit(125);
        close(marker);
        _exit(47);
    }
    return write(fd, buffer, count);
}

static ssize_t observed_read(int fd, void *buffer, size_t count) {
    ssize_t result = read(fd, buffer, count);
    if (selected && fd == STDIN_FILENO && result == 0 && !atomic_exchange(&eof, true)) {
        int observed = open(getenv("MCP_CONSOLE_TEST_STARTUP_EOF"), O_WRONLY);
        if (observed < 0 || write(observed, "1", 1) != 1) _exit(124);
        close(observed);
    }
    return result;
}

static void checkpoint_selection(const char *path) {
    if (selected && path && strcmp(path, selected) == 0 && !atomic_exchange(&used, true)) {
        int reached = open(getenv("MCP_CONSOLE_TEST_STARTUP_STAGE_REACHED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_STARTUP_STAGE_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(122);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(123);
        close(reached);
        close(release);
    }
}

static int observed_lstat(const char *path, struct stat *status) {
    checkpoint_selection(path);
    return lstat(path, status);
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} interpose_lstat __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_lstat, (const void *)(uintptr_t)&lstat,
};
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} interpose_read __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_read, (const void *)(uintptr_t)&read,
};
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_write, (const void *)(uintptr_t)&write,
};
#else
#undef lstat
#undef read
#undef write
int lstat(const char *path, struct stat *status) { return observed_lstat(path, status); }
// Rust uses statx for Linux symlink_metadata, and lstat on macOS.
int statx(int directory, const char *path, int flags, unsigned int mask, struct statx *status) {
    checkpoint_selection(path);
    return native_statx(directory, path, flags, mask, status);
}
ssize_t read(int fd, void *buffer, size_t count) { return observed_read(fd, buffer, count); }
ssize_t write(int fd, const void *buffer, size_t count) { return observed_write(fd, buffer, count); }
#endif
