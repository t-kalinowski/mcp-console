#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
#include <linux/futex.h>
#include <stdarg.h>
#include <sys/syscall.h>
static pid_t (*native_fork)(void);
static long (*native_syscall)(long, ...);
#endif

static pid_t server_pid;
static atomic_bool first_fork = false;
static atomic_uintptr_t waiting_mutex = 0;
static atomic_uintptr_t contended_mutex = 0;
static atomic_bool cancelling = false;
static atomic_bool paused = false;
static _Thread_local bool initial_evaluator = false;
static _Thread_local bool released = false;

__attribute__((constructor)) static void initialize(void) {
    server_pid = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
#ifdef __linux__
    native_fork = dlsym(RTLD_NEXT, "fork");
    native_syscall = dlsym(RTLD_NEXT, "syscall");
    if (native_fork == NULL || native_syscall == NULL) _exit(120);
#endif
}

static void notify(const char *name) {
    int descriptor = open(getenv(name), O_WRONLY | O_NONBLOCK);
    if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(121);
    close(descriptor);
}

static pid_t observe_fork(void) {
    if (getpid() == server_pid && !atomic_exchange(&first_fork, true)) {
        initial_evaluator = true;
    }
#ifdef __APPLE__
    return fork();
#else
    return native_fork();
#endif
}

static void select_worker_mutex(uintptr_t mutex) {
    uintptr_t unset = 0;
    if (mutex != 0 && atomic_compare_exchange_strong(&contended_mutex, &unset, mutex)) {
        notify("MCP_CONSOLE_TEST_COMPLETION_CONTENDED");
    }
}

static void observe_contention(uintptr_t mutex) {
    if (getpid() != server_pid || initial_evaluator ||
        access(getenv("MCP_CONSOLE_TEST_COMPLETION_ARMED"), F_OK) != 0) return;
    // Discard completed waits: restart can briefly contend on lifecycle
    // admission before sending cancellation and waiting for the worker.
    uintptr_t unset = 0;
    if (atomic_compare_exchange_strong(&waiting_mutex, &unset, mutex) &&
        atomic_load(&cancelling)) select_worker_mutex(mutex);
}

static void after_contention(uintptr_t mutex) {
    atomic_compare_exchange_strong(&waiting_mutex, &mutex, 0);
}

static void await_release(const char *name) {
    int descriptor = open(getenv(name), O_RDONLY);
    if (descriptor < 0) _exit(122);
    char token;
    ssize_t count;
    do {
        count = read(descriptor, &token, 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(123);
    close(descriptor);
}

static int observe_killpg(pid_t group, int number) {
    if (getpid() == server_pid && initial_evaluator && number == SIGKILL &&
        access(getenv("MCP_CONSOLE_TEST_COMPLETION_ARMED"), F_OK) == 0 &&
        !atomic_exchange(&cancelling, true)) {
        // Cancellation has left lifecycle admission. Keep the evaluator in
        // resolver cleanup until restart contends on the worker mutex. This
        // also guarantees a Linux futex wake at the eventual worker unlock.
        select_worker_mutex(atomic_load(&waiting_mutex));
        await_release("MCP_CONSOLE_TEST_COMPLETION_CANCEL_RELEASE");
    }
    return kill(-group, number);
}

static void after_unlock(uintptr_t mutex) {
    if (getpid() != server_pid || !initial_evaluator ||
        mutex != atomic_load(&contended_mutex) || atomic_exchange(&paused, true)) return;
    notify("MCP_CONSOLE_TEST_COMPLETION_UNLOCKED");
    await_release("MCP_CONSOLE_TEST_COMPLETION_RELEASE");
    released = true;
}

static void before_park(void) {
    // The replacement response is delivered before the test releases this
    // thread. Its next pool wait proves the old evaluation task has returned.
    if (getpid() == server_pid && released) {
        released = false;
        notify("MCP_CONSOLE_TEST_COMPLETION_PARKED");
    }
}

#ifdef __APPLE__
static int observe_mutex_lock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_trylock(mutex);
    if (result != EBUSY) return result;
    observe_contention((uintptr_t)mutex);
    result = pthread_mutex_lock(mutex);
    after_contention((uintptr_t)mutex);
    return result;
}

static int observe_mutex_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result == 0) after_unlock((uintptr_t)mutex);
    return result;
}

static int observe_cond_timedwait_relative(pthread_cond_t *condition, pthread_mutex_t *mutex,
                                           const struct timespec *timeout) {
    before_park();
    return pthread_cond_timedwait_relative_np(condition, mutex, timeout);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observe_fork, fork)
DYLD_INTERPOSE(observe_killpg, killpg)
DYLD_INTERPOSE(observe_mutex_lock, pthread_mutex_lock)
DYLD_INTERPOSE(observe_mutex_unlock, pthread_mutex_unlock)
DYLD_INTERPOSE(observe_cond_timedwait_relative, pthread_cond_timedwait_relative_np)
#else
pid_t fork(void) { return observe_fork(); }
int killpg(pid_t group, int number) { return observe_killpg(group, number); }

long syscall(long number, ...) {
    // Forward libc's six argument slots, as in the relay queue checkpoint.
    va_list arguments;
    va_start(arguments, number);
    long slots[6];
    for (int index = 0; index < 6; ++index) slots[index] = va_arg(arguments, long);
    va_end(arguments);
    long command = number == SYS_futex ? slots[1] & FUTEX_CMD_MASK : -1;
    if (command == FUTEX_WAIT || command == FUTEX_WAIT_BITSET) {
        // Tokio's idle blocking-pool wait has a timeout; mutex waits do not.
        if (slots[3] != 0) before_park();
        if (slots[2] == 2) observe_contention((uintptr_t)slots[0]);
    }
    long result = native_syscall(number, slots[0], slots[1], slots[2], slots[3], slots[4], slots[5]);
    if (command == FUTEX_WAIT || command == FUTEX_WAIT_BITSET) {
        after_contention((uintptr_t)slots[0]);
    }
    if (command == FUTEX_WAKE) after_unlock((uintptr_t)slots[0]);
    return result;
}
#endif
