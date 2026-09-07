#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static atomic_bool completed = false;
static struct stat output_identity;
static int completion_record;

__attribute__((constructor)) static void initialize_clock(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    completion_record = open(getenv("MCP_CONSOLE_TEST_OUTPUT_COMPLETE"),
                             O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (completion_record < 0 || fstat(STDOUT_FILENO, &output_identity) < 0) {
        _exit(91);
    }
}

static ssize_t observe_final_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    struct stat identity;
    if (result <= 0 || fstat(descriptor, &identity) < 0 ||
        identity.st_dev != output_identity.st_dev ||
        identity.st_ino != output_identity.st_ino) {
        return result;
    }
    static const char final_frame[] = "{\"kind\":\"worker_exited\",\"code\":0}\n";
    static size_t matched = 0;
    const char *bytes = buffer;
    for (ssize_t index = 0; index < result; ++index) {
        matched = bytes[index] == final_frame[matched]
                      ? matched + 1
                      : (bytes[index] == final_frame[0] ? 1 : 0);
        if (matched == sizeof(final_frame) - 1) {
            if (write(completion_record, "1", 1) != 1) {
                _exit(92);
            }
            atomic_store(&completed, true);
            matched = 0;
        }
    }
    return result;
}

// Move past the retirement deadline only after the final complete frame is in
// the pipe. This models descheduling between the final write and a no-op flush.
static int advance_after_output(clockid_t clock, struct timespec *time) {
    int result = clock_gettime(clock, time);
    if (result == 0 && clock != CLOCK_REALTIME && atomic_load(&completed)) {
        time->tv_sec += 2;
    }
    return result;
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                              \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observe_final_write, write)
DYLD_INTERPOSE(advance_after_output, clock_gettime)
