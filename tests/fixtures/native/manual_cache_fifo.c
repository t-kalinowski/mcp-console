#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static bool replaced;

static void replace_cache_file(const char *path) {
    const char *target = getenv("CONSOLE_TEST_CACHE_OPEN_TARGET");
    if (replaced || target == NULL || strcmp(path, target) != 0) return;
    replaced = true;
    // The caller already inspected the regular directory entry. Replace it
    // exactly at open, so this public regression does not depend on a race loop.
    if (unlink(path) < 0 || mkfifo(path, 0600) < 0) _exit(125);
    int marker = open(getenv("CONSOLE_TEST_CACHE_OPEN_MARKER"), O_WRONLY | O_CREAT, 0600);
    if (marker < 0 || write(marker, "1", 1) != 1 || close(marker) < 0) _exit(125);
}

static int cache_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list arguments;
        va_start(arguments, flags);
        mode = (mode_t)va_arg(arguments, int);
        va_end(arguments);
    }
    replace_cache_file(path);
#ifdef __APPLE__
    return open(path, flags, mode);
#else
    return ((int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open"))(path, flags, mode);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *original; }
interpose_open __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&cache_open, (const void *)(uintptr_t)&open
};
#else
int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list arguments;
        va_start(arguments, flags);
        mode = (mode_t)va_arg(arguments, int);
        va_end(arguments);
    }
    return cache_open(path, flags, mode);
}
int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list arguments;
        va_start(arguments, flags);
        mode = (mode_t)va_arg(arguments, int);
        va_end(arguments);
    }
    return cache_open(path, flags, mode);
}
#endif
