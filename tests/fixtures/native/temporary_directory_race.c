#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *root;
static const char *race;
static bool server;

__attribute__((constructor)) static void initialize(int argc, char **argv) {
    root = getenv("MCP_CONSOLE_TEST_STORAGE_RACE_ROOT");
    race = getenv("MCP_CONSOLE_TEST_STORAGE_RACE");
    server = argc > 1 && strcmp(argv[1], "serve") == 0;
    if (argc > 1 && strcmp(argv[1], "worker-relay") == 0) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static bool armed(const char *kind) {
    if (!server || root == NULL || race == NULL || strcmp(race, kind) != 0) return false;
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/armed", root) >= (int)sizeof(path)) _exit(125);
    if (unlink(path) == 0) return true;
    if (errno != ENOENT) _exit(125);
    return false;
}

static void receipt(void) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/mutated", root) >= (int)sizeof(path)) _exit(125);
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor < 0 || write(descriptor, race, strlen(race)) != (ssize_t)strlen(race)) _exit(125);
    close(descriptor);
}

static void replace_with_symlink(int parent, const char *path) {
    const char *name = strrchr(path, '/');
    if (strcmp(name == NULL ? path : name + 1, "raced") != 0 || !armed("symlink")) return;
    char saved[4096], external[4096];
    if (snprintf(saved, sizeof(saved), "%s-saved", path) >= (int)sizeof(saved) ||
        snprintf(external, sizeof(external), "%s/external-library", root) >= (int)sizeof(external)) _exit(125);
    if (renameat(parent, path, parent, saved) < 0 || symlinkat(external, parent, path) < 0) _exit(125);
    receipt();
}

static int race_chmod(const char *path, mode_t mode) {
    replace_with_symlink(AT_FDCWD, path);
#ifdef __APPLE__
    return chmod(path, mode);
#else
    return ((int (*)(const char *, mode_t))dlsym(RTLD_NEXT, "chmod"))(path, mode);
#endif
}

static int race_fchmodat(int parent, const char *path, mode_t mode, int flags) {
    replace_with_symlink(parent, path);
#ifdef __APPLE__
    return fchmodat(parent, path, mode, flags);
#else
    return ((int (*)(int, const char *, mode_t, int))dlsym(RTLD_NEXT, "fchmodat"))(parent, path, mode, flags);
#endif
}

static struct dirent *race_readdir(DIR *directory) {
#ifdef __APPLE__
    struct dirent *entry = readdir(directory);
#else
    struct dirent *entry = ((struct dirent *(*)(DIR *))dlsym(RTLD_NEXT, "readdir"))(directory);
#endif
    if (entry != NULL && strcmp(entry->d_name, "vanishing") == 0 && armed("vanished")) {
        if (unlinkat(dirfd(directory), entry->d_name, AT_REMOVEDIR) < 0) _exit(125);
        receipt();
    }
    return entry;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(race_chmod, chmod)
INTERPOSE(race_fchmodat, fchmodat)
INTERPOSE(race_readdir, readdir)
#else
int chmod(const char *path, mode_t mode) { return race_chmod(path, mode); }
int fchmodat(int parent, const char *path, mode_t mode, int flags) { return race_fchmodat(parent, path, mode, flags); }
struct dirent *readdir(DIR *directory) { return race_readdir(directory); }
struct dirent64 *readdir64(DIR *directory) {
    struct dirent64 *entry = ((struct dirent64 *(*)(DIR *))dlsym(RTLD_NEXT, "readdir64"))(directory);
    if (entry != NULL && strcmp(entry->d_name, "vanishing") == 0 && armed("vanished")) {
        if (unlinkat(dirfd(directory), entry->d_name, AT_REMOVEDIR) < 0) _exit(125);
        receipt();
    }
    return entry;
}
#endif
