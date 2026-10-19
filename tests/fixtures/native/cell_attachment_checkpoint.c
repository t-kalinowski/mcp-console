#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Thread_local pthread_mutex_t *locks[64];
static _Thread_local size_t lock_count;
static _Thread_local pthread_mutex_t *attachment;
static int selected;

__attribute__((constructor)) static void initialize(void) {
    // This gate belongs to the server, not its resolver or worker children.
    if (unsetenv("DYLD_INSERT_LIBRARIES") != 0) _exit(120);
}

static int observed_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_lock(mutex);
    if (result == 0) {
        if (lock_count == 64) _exit(121);
        locks[lock_count++] = mutex;
    }
    return result;
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    const char marker[] = "{\"event\":\"source_attached\",";
    if (result == (ssize_t)length && length >= sizeof(marker) - 1 &&
        memcmp(buffer, marker, sizeof(marker) - 1) == 0 &&
        __atomic_exchange_n(&selected, 1, __ATOMIC_RELAXED) == 0) {
        // Cell attachment holds the operation lock outside the transcript lock.
        // Stop only after it is released and the Cell route has been installed.
        if (lock_count < 2) _exit(122);
        attachment = locks[lock_count - 2];
    }
    return result;
}

static int observed_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result != 0) return result;
    if (lock_count != 0 && locks[lock_count - 1] == mutex) --lock_count;
    if (mutex == attachment) {
        attachment = NULL;
        int reached = open(getenv("MCP_CONSOLE_TEST_CELL_ATTACHED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_CELL_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(123);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(124);
        close(reached);
        close(release);
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
INTERPOSE(observed_unlock, pthread_mutex_unlock)
INTERPOSE(observed_write, write)
