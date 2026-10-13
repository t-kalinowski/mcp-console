#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static char replaced[PATH_MAX];
static char inspected[PATH_MAX];
static int visits;

static void replace_directory(const char *path) {
    const char *target = getenv("CONSOLE_TEST_CACHE_DIRECTORY");
    if (target == NULL || strcmp(path, target) != 0 || strcmp(replaced, target) == 0) return;
    if (strcmp(inspected, target) != 0) {
        strcpy(inspected, target);
        visits = 0;
    }
    if (visits++ < atoi(getenv("CONSOLE_TEST_CACHE_DIRECTORY_SKIP"))) return;
    char moved[PATH_MAX];
    if (snprintf(moved, sizeof(moved), "%s.moved", path) >= (int)sizeof(moved)) _exit(125);
    if (rename(path, moved) < 0 || symlink(getenv("CONSOLE_TEST_CACHE_LINK_TARGET"), path) < 0) _exit(125);
    strcpy(replaced, target);
    int marker = open(getenv("CONSOLE_TEST_CACHE_DIRECTORY_MARKER"), O_WRONLY | O_CREAT, 0600);
    if (marker < 0 || write(marker, "1", 1) != 1 || close(marker) < 0) _exit(125);
}

static DIR *cache_opendir(const char *path) {
    // Old path-based traversal resolves the replacement symlink here.
    replace_directory(path);
#ifdef __APPLE__
    return opendir(path);
#else
    return ((DIR *(*)(const char *))dlsym(RTLD_NEXT, "opendir"))(path);
#endif
}

static DIR *cache_fdopendir(int fd) {
    // A descriptor-based traversal must stay on the opened directory even
    // when its former name is replaced before enumeration.
    char path[PATH_MAX];
#ifdef __APPLE__
    if (fcntl(fd, F_GETPATH, path) < 0) _exit(125);
#else
    char descriptor[64];
    snprintf(descriptor, sizeof(descriptor), "/proc/self/fd/%d", fd);
    ssize_t length = readlink(descriptor, path, sizeof(path) - 1);
    if (length < 0) _exit(125);
    path[length] = '\0';
#endif
    replace_directory(path);
#ifdef __APPLE__
    return fdopendir(fd);
#else
    return ((DIR *(*)(int))dlsym(RTLD_NEXT, "fdopendir"))(fd);
#endif
}

#ifdef __APPLE__
#define INTERPOSE(name) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##name __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&cache_##name, (const void *)(uintptr_t)&name \
    }
INTERPOSE(opendir);
INTERPOSE(fdopendir);
#else
DIR *opendir(const char *path) { return cache_opendir(path); }
DIR *fdopendir(int fd) { return cache_fdopendir(fd); }
#endif
