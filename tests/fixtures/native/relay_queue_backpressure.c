#include "relay_stdout_backpressure.c"

#include <pthread.h>
#include <stdio.h>
#include <sys/socket.h>

static atomic_int sideband_descriptor = -1;
static _Thread_local bool is_sideband_reader = false;
static _Thread_local bool capacity_reported = false;
static _Thread_local size_t consumed_bytes = 0;
static _Thread_local size_t consumed_frames = 0;

static int observed_socketpair(int domain, int type, int protocol, int pair[2]) {
    int result = socketpair(domain, type, protocol, pair);
    if (result == 0 && domain == AF_UNIX && type == SOCK_STREAM) {
        int unassigned = -1;
        atomic_compare_exchange_strong(&sideband_descriptor, &unassigned, pair[0]);
    }
    return result;
}

static ssize_t record_read(int descriptor, const void *buffer, ssize_t result) {
    if (result > 0 && descriptor == atomic_load(&sideband_descriptor)) {
        is_sideband_reader = true;
        consumed_bytes += (size_t)result;
        const unsigned char *bytes = buffer;
        for (ssize_t index = 0; index < result; ++index) {
            consumed_frames += bytes[index] == '\n';
        }
    }
    return result;
}

static ssize_t observed_read(int descriptor, void *buffer, size_t length) {
    return record_read(descriptor, buffer, read(descriptor, buffer, length));
}

static ssize_t observed_recv(int descriptor, void *buffer, size_t length, int flags) {
    return record_read(descriptor, buffer, recv(descriptor, buffer, length, flags));
}

// READY may briefly occupy the budget before the first output frame. The worker
// gates subsequent frames until the parent observes that first blocked write.
static void notify_capacity_wait(void) {
    if (!is_sideband_reader || capacity_reported || !atomic_load(&observed)) {
        return;
    }
    capacity_reported = true;
    char counts[100];
    int length = snprintf(counts, sizeof(counts), "%zu %zu\n", consumed_bytes,
                          consumed_frames);
    int record = open(getenv("MCP_CONSOLE_TEST_QUEUE_COUNTERS"),
                      O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (record < 0 || write(record, counts, (size_t)length) != length) {
        _exit(97);
    }
    close(record);
    int checkpoint = open(getenv("MCP_CONSOLE_TEST_QUEUE_WAIT"),
                          O_WRONLY | O_CLOEXEC);
    if (checkpoint < 0 || write(checkpoint, "1", 1) != 1) {
        _exit(98);
    }
    close(checkpoint);
}

// Observe the real blocking boundary without holding a separate gate before
// pthread_cond_wait atomically releases the queue's associated mutex.
static int observed_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    notify_capacity_wait();
    return pthread_cond_wait(condition, mutex);
}

static int observed_cond_timedwait(pthread_cond_t *condition, pthread_mutex_t *mutex,
                                  const struct timespec *deadline) {
    notify_capacity_wait();
    return pthread_cond_timedwait(condition, mutex, deadline);
}

DYLD_INTERPOSE(observed_socketpair, socketpair)
DYLD_INTERPOSE(observed_read, read)
DYLD_INTERPOSE(observed_recv, recv)
DYLD_INTERPOSE(observed_cond_wait, pthread_cond_wait)
DYLD_INTERPOSE(observed_cond_timedwait, pthread_cond_timedwait)
