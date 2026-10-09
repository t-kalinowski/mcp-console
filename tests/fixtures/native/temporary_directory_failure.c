#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

static int fail_unlinkat(int directory, const char *path, int flags) {
    // Read-only directories are recoverable. Inject an actual removal failure
    // at the fixture-owned child to exercise unconfirmed storage retirement.
    if ((flags & AT_REMOVEDIR) && strcmp(path, "restricted") == 0) {
        errno = EACCES;
        return -1;
    }
#ifdef __APPLE__
    return unlinkat(directory, path, flags);
#else
    return ((int (*)(int, const char *, int))dlsym(RTLD_NEXT, "unlinkat"))(
        directory, path, flags);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} unlinkat_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)fail_unlinkat, (const void *)unlinkat,
};
#else
int unlinkat(int directory, const char *path, int flags) {
    return fail_unlinkat(directory, path, flags);
}
#endif
