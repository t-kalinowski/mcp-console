#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void record(const char *name, const char *message) {
    const char *path = getenv(name);
    if (path == NULL) return;
    int file = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (file >= 0) {
        (void)write(file, message, strlen(message));
        close(file);
    }
}

__attribute__((constructor)) static void startup(void) {
    record("RESOLVER_PROBE_OUTSIDE", "startup\n");
    record("RESOLVER_PROBE_INSIDE", "startup\n");
    const char *secret = getenv("RESOLVER_PROBE_SECRET");
    if (secret == NULL) return;
    int file = open(secret, O_RDONLY);
    if (file >= 0) {
        record("RESOLVER_PROBE_INSIDE", "secret-readable\n");
        close(file);
    }
}
