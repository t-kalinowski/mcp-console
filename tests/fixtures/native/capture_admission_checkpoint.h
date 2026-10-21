#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static _Thread_local pthread_mutex_t *locks[64];
static _Thread_local size_t lock_count;
static _Thread_local pthread_mutex_t *attachment;
static atomic_bool armed;
static atomic_bool selected;
static atomic_bool verified;
static char admission_at[64];

static bool contains(const void *buffer, size_t length, const char *text) {
    size_t size = strlen(text);
    for (size_t offset = 0; offset + size <= length; ++offset)
        if (memcmp((const char *)buffer + offset, text, size) == 0) return true;
    return false;
}

static int observed_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (lock_count == 64) _exit(130);
        locks[lock_count++] = mutex;
    }
    return result;
}

static void unlocked(pthread_mutex_t *mutex) {
    if (lock_count != 0 && locks[lock_count - 1] == mutex) --lock_count;
}

static int observed_clock(clockid_t clock, struct timespec *value) {
    int result = clock_gettime(clock, value);
    if (result == 0 && clock == CLOCK_REALTIME && atomic_load(&armed) &&
        lock_count == 3 && !atomic_exchange(&selected, true)) {
        // Attachment observes its timestamp while holding the worker,
        // operation, and capture-queue mutexes. Select the operation boundary
        // on its producer, independently of the later journal writer.
        attachment = locks[1];
        struct tm utc;
        if (gmtime_r(&value->tv_sec, &utc) == NULL) _exit(131);
        size_t end = strftime(admission_at, sizeof(admission_at), "%Y-%m-%dT%H:%M:%S", &utc);
        if (end == 0) _exit(132);
        snprintf(admission_at + end, sizeof(admission_at) - end, ".%09ldZ", value->tv_nsec);
    }
    return result;
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    if (result != (ssize_t)length) return result;
    const char call[] = "{\"event\":\"tool_call\",";
    const char attached[] = "{\"event\":\"source_attached\",";
    if (length >= sizeof(call) - 1 && memcmp(buffer, call, sizeof(call) - 1) == 0 &&
        contains(buffer, length, getenv("MCP_CONSOLE_TEST_ADMISSION_SOURCE")))
        atomic_store(&armed, true);
    if (length >= sizeof(attached) - 1 && memcmp(buffer, attached, sizeof(attached) - 1) == 0 &&
        !atomic_exchange(&verified, true)) {
        // Fail if the structural gate selected any observation other than this
        // exact attachment. Journal scheduling must not choose the evaluator.
        if (!atomic_load(&selected) || !contains(buffer, length, admission_at)) _exit(133);
    }
    return result;
}

#define INTERPOSE(replacement, original)                                      \
    __attribute__((used)) static struct {                                     \
        const void *replacement;                                             \
        const void *original;                                                \
    } interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                                \
        (const void *)(uintptr_t)&original,                                   \
    };
INTERPOSE(observed_lock, pthread_mutex_lock)
INTERPOSE(observed_clock, clock_gettime)
INTERPOSE(observed_write, write)
