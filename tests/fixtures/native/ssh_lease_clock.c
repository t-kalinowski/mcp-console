#ifdef __linux__
#define _GNU_SOURCE
#endif

#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_read)(int, void *, size_t);
#define read native_read
static int (*native_clock_gettime)(clockid_t, struct timespec *);
#define clock_gettime native_clock_gettime
#endif

static bool selected;
static atomic_bool advanced;
static unsigned tag, ordinal, count;
static long milliseconds;
static const char *record;
static struct {
    dev_t device;
    ino_t inode;
    unsigned char header[5];
    unsigned used, remaining;
    bool invalid;
} streams[1024];

__attribute__((constructor)) static void initialize(int argc, char **argv) {
#ifdef __linux__
    native_read = dlsym(RTLD_NEXT, "read");
    native_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    if (native_read == NULL || native_clock_gettime == NULL) _exit(90);
#endif
    const char *role = getenv("MCP_CONSOLE_TEST_SSH_CLOCK_ROLE");
    selected = role != NULL && argc > 1 && strcmp(argv[1], role) == 0;
    if (!selected) return;
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    tag = strtoul(getenv("MCP_CONSOLE_TEST_SSH_CLOCK_TAG"), NULL, 10);
    ordinal = strtoul(getenv("MCP_CONSOLE_TEST_SSH_CLOCK_ORDINAL"), NULL, 10);
    milliseconds = strtol(getenv("MCP_CONSOLE_TEST_SSH_CLOCK_MS"), NULL, 10);
    record = getenv("MCP_CONSOLE_TEST_SSH_CLOCK_RECORD");
}

// Observe complete outer frames, including fragmented headers and bodies. The
// selected owner has one I/O loop; child helpers do not inherit this fixture.
static ssize_t observe_read(int fd, void *buffer, size_t length) {
    ssize_t result = read(fd, buffer, length);
    if (!selected || result <= 0 || atomic_load(&advanced) || fd < 0 || fd >= 1024)
        return result;
    struct stat identity;
    if (fstat(fd, &identity) < 0) _exit(91);
    if (streams[fd].device != identity.st_dev || streams[fd].inode != identity.st_ino) {
        memset(&streams[fd], 0, sizeof streams[fd]);
        streams[fd].device = identity.st_dev;
        streams[fd].inode = identity.st_ino;
    }
    const unsigned char *bytes = buffer;
    for (ssize_t i = 0; i < result && !streams[fd].invalid; ++i) {
        if (streams[fd].used < 5) {
            streams[fd].header[streams[fd].used++] = bytes[i];
            if (streams[fd].used < 5) continue;
            const unsigned char *h = streams[fd].header;
            streams[fd].remaining = ((unsigned)h[1] << 24) | ((unsigned)h[2] << 16)
                | ((unsigned)h[3] << 8) | h[4];
            if (h[0] == 0 || h[0] > 10 || streams[fd].remaining > 32768) {
                streams[fd].invalid = true; // Not a lease protocol descriptor.
                continue;
            }
        } else {
            --streams[fd].remaining;
        }
        if (streams[fd].remaining == 0) {
            if (streams[fd].header[0] == tag && ++count == ordinal) {
                int output = open(record, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
                if (output < 0 || write(output, "1", 1) != 1) _exit(92);
                close(output);
                atomic_store(&advanced, true);
            }
            streams[fd].used = 0;
        }
    }
    return result;
}

static int advance_clock(clockid_t clock, struct timespec *time) {
    int result = clock_gettime(clock, time);
    if (result == 0 && clock != CLOCK_REALTIME && atomic_load(&advanced)) {
        time->tv_sec += milliseconds / 1000;
        time->tv_nsec += (milliseconds % 1000) * 1000000;
        time->tv_sec += time->tv_nsec / 1000000000;
        time->tv_nsec %= 1000000000;
    }
    return result;
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn; const void *old_fn; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original };
DYLD_INTERPOSE(observe_read, read)
DYLD_INTERPOSE(advance_clock, clock_gettime)
#else
#undef read
ssize_t read(int fd, void *buffer, size_t length) { return observe_read(fd, buffer, length); }
#undef clock_gettime
int clock_gettime(clockid_t clock, struct timespec *time) { return advance_clock(clock, time); }
#endif
