#include "relay_stdout_backpressure.c"

#include <pthread.h>
#include <stdio.h>
#include <sys/socket.h>
#ifdef __linux__
#include <linux/futex.h>
#include <stdarg.h>
#include <sys/syscall.h>
#endif

#ifdef __linux__
static int (*next_socketpair)(int domain, int type, int protocol, int pair[2]);
#define socketpair next_socketpair
static ssize_t (*next_read)(int descriptor, void *buffer, size_t length);
#define read next_read
static ssize_t (*next_recv)(int descriptor, void *buffer, size_t length, int flags);
#define recv next_recv
static long (*next_syscall)(long, ...);
__attribute__((constructor)) static void initialize_queue_interposition(void) {
    next_socketpair = dlsym(RTLD_NEXT, "socketpair");
    if (next_socketpair == NULL) _exit(90);
    next_read = dlsym(RTLD_NEXT, "read");
    if (next_read == NULL) _exit(90);
    next_recv = dlsym(RTLD_NEXT, "recv");
    if (next_recv == NULL) _exit(90);
    next_syscall = dlsym(RTLD_NEXT, "syscall");
    if (next_syscall == NULL) _exit(90);
}
#endif


static atomic_int sideband_descriptor = -1;
static _Thread_local bool is_sideband_reader = false;
static _Thread_local bool capacity_reported = false;
static _Thread_local size_t consumed_bytes = 0;
static _Thread_local size_t consumed_frames = 0;

static int observed_socketpair(int domain, int type, int protocol, int pair[2]) {
    int result = socketpair(domain, type, protocol, pair);
#ifdef __linux__
    type &= ~(SOCK_CLOEXEC | SOCK_NONBLOCK);
#endif
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
#ifdef __APPLE__
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

#else
#undef socketpair
int socketpair(int domain, int type, int protocol, int pair[2]) {
    return observed_socketpair(domain, type, protocol, pair);
}
#undef read
ssize_t read(int descriptor, void *buffer, size_t length) {
    return observed_read(descriptor, buffer, length);
}
#undef recv
ssize_t recv(int descriptor, void *buffer, size_t length, int flags) {
    return observed_recv(descriptor, buffer, length, flags);
}
long syscall(long number, ...) {
    // Rust's Linux condition variable waits directly on a futex. Forward the
    // six syscall argument slots using libc's syscall ABI, including unused
    // slots; only the kernel interprets the arguments for the selected call.
    va_list arguments;
    va_start(arguments, number);
    long slots[6];
    for (int index = 0; index < 6; ++index) slots[index] = va_arg(arguments, long);
    va_end(arguments);
    if (number == SYS_futex &&
        ((slots[1] & FUTEX_CMD_MASK) == FUTEX_WAIT ||
         (slots[1] & FUTEX_CMD_MASK) == FUTEX_WAIT_BITSET)) {
        notify_capacity_wait();
    }
    return next_syscall(number, slots[0], slots[1], slots[2], slots[3], slots[4], slots[5]);
}
#endif
