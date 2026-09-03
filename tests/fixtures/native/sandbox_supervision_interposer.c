#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/event.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int denied_sigkill = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
#define MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY 1
static _Atomic pid_t retirement_root = 0;
static _Atomic pid_t retirement_descendant = 0;
static _Atomic int retirement_descendant_observed = 0;
static _Atomic int retirement_root_exited = 0;
static _Atomic int retirement_descendant_snapshotted = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY)
static _Atomic int retirement_identity_changed = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
static _Atomic int retirement_signal_gated = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static _Atomic int manager_group_stop_started = 0;
static _Atomic int manager_root_stop_reported = 0;
static _Atomic pid_t manager_observed_root = 0;
static _Atomic pid_t manager_observed_descendant = 0;
static _Atomic int manager_descendant_observed_reported = 0;
static _Atomic int manager_descendant_stop_reported = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int delayed_late_recovery = 0;
static _Atomic int reaped_root = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
static _Atomic int gated_manager_group_cleanup = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static _Atomic int gated_manager_start = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER)
static _Atomic int sandbox_fork_count = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE)
static _Atomic int failed_owner_monitor_start = 0;
static _Atomic int gated_owner_manager_stop = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static _Atomic int failed_process_info = 0;
static _Atomic int failed_group_stop = 0;
static _Atomic int gated_recovery_root_stop = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static _Atomic int root_exit_watch_registered = 0;
static _Atomic int failed_root_identity_recheck = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
typedef int (*kill_function)(pid_t, int);

static kill_function next_kill(void) {
    return kill;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
typedef int (*killpg_function)(pid_t, int);

static killpg_function next_killpg(void) {
    return killpg;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY)
typedef int (*proc_pidinfo_function)(int, int, uint64_t, void *, int);

static proc_pidinfo_function next_proc_pidinfo(void) {
    return proc_pidinfo;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY)
typedef int (*kevent_function)(
    int,
    const struct kevent *,
    int,
    struct kevent *,
    int,
    const struct timespec *
);

static kevent_function next_kevent(void) {
    return kevent;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY)
typedef int (*proc_listchildpids_function)(pid_t, void *, int);

static proc_listchildpids_function next_proc_listchildpids(void) {
    return proc_listchildpids;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static void signal_checkpoint(const char *name);

static int arm_root_identity_recheck(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    int result = next_kevent()(
        descriptor,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
    if (result >= 0
        && change_count == 1
        && changes != NULL
        && changes[0].filter == EVFILT_PROC
        && (changes[0].flags & EV_ADD) != 0
        && (changes[0].fflags & NOTE_EXIT) != 0) {
        atomic_store(&root_exit_watch_registered, 1);
    }
    return result;
}

static int fail_root_observer(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && atomic_load(&root_exit_watch_registered) != 0
        && atomic_exchange(&failed_root_identity_recheck, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE");
        errno = EIO;
        return 0;
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}

static int fail_root_group_stop(pid_t process_id, int number) {
    if (process_id < 0 && number == SIGKILL) {
        signal_checkpoint("MCP_CONSOLE_TEST_GROUP_STOP_FAILURE");
        errno = EIO;
        return -1;
    }
    return next_kill()(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static void signal_checkpoint(const char *name);

static int fail_group_stop(pid_t process_group_id, int number) {
    const char *trigger = getenv("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER");
    if (number == SIGKILL && trigger != NULL && access(trigger, F_OK) == 0) {
        atomic_store(&failed_group_stop, 1);
        signal_checkpoint("MCP_CONSOLE_TEST_GROUP_STOP_FAILURE");
        errno = EIO;
        return -1;
    }
    return killpg(process_group_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
typedef pid_t (*waitpid_function)(pid_t, int *, int);

static waitpid_function next_waitpid(void) {
    return waitpid;
}
#endif

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        return;
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor >= 0) {
        const char value = '1';
        (void)write(descriptor, &value, sizeof(value));
        close(descriptor);
    }
}

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER) \
    || defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
static void wait_for_release(const char *name) {
    const char *release = getenv(name);
    if (release == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(release, O_RDONLY);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }
    char value;
    ssize_t count;
    do {
        count = read(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static int fail_process_info(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    const char *trigger = getenv("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER");
    if (flavor == PROC_PIDTBSDINFO
        && trigger != NULL
        && access(trigger, F_OK) == 0) {
        if (atomic_exchange(&failed_process_info, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE");
        }
        errno = EIO;
        return 0;
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}

static int gate_recovery_root_stop(pid_t process_id, int number) {
    kill_function kill_next = next_kill();
    if (process_id > 0
        && number == SIGKILL
        && atomic_load(&failed_group_stop) != 0
        && getenv("MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED") != NULL
        && atomic_exchange(&gated_recovery_root_stop, 1) == 0) {
        int result = kill_next(process_id, number);
        if (result == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED");
            wait_for_release("MCP_CONSOLE_TEST_RECOVERY_ROOT_RELEASE");
        }
        return result;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER) \
    || defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY)
static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY)
static int observe_retirement_processes(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    if (is_subcommand("sandbox-manager")
        && changes == NULL
        && change_count == 0
        && events != NULL
        && event_count > 0
        && timeout == NULL
        && atomic_load(&retirement_descendant) != 0
        && atomic_exchange(&retirement_descendant_observed, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_DESCENDANT_OBSERVED");
    }

    int result = next_kevent()(
        descriptor,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
    if (result < 0 || !is_subcommand("sandbox-manager")) {
        return result;
    }

    if (changes != NULL
        && change_count == 1
        && changes[0].filter == EVFILT_PROC
        && (changes[0].flags & EV_ADD) != 0
        && (changes[0].fflags & NOTE_EXIT) != 0) {
        pid_t process_id = (pid_t)changes[0].ident;
        pid_t root = atomic_load(&retirement_root);
        if (root == 0) {
            atomic_store(&retirement_root, process_id);
        } else if (process_id != root
            && atomic_load(&retirement_descendant) == 0) {
            atomic_store(&retirement_descendant, process_id);
        }
    }

    if (events != NULL) {
        pid_t root = atomic_load(&retirement_root);
        for (int index = 0; index < result; index++) {
            if (events[index].filter == EVFILT_PROC
                && (pid_t)events[index].ident == root
                && (events[index].fflags & NOTE_EXIT) != 0) {
                atomic_store(&retirement_root_exited, 1);
            }
        }
    }
    return result;
}

static int observe_retirement_child_snapshot(
    pid_t process_id,
    void *buffer,
    int buffer_size
) {
    int result = next_proc_listchildpids()(process_id, buffer, buffer_size);
    if (is_subcommand("sandbox-manager")
        && atomic_load(&retirement_root_exited) != 0
        && process_id == atomic_load(&retirement_descendant)) {
        atomic_store(&retirement_descendant_snapshotted, 1);
    }
    return result;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY)
static int reuse_retirement_descendant_identity(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    int result = next_proc_pidinfo()(
        process_id,
        flavor,
        argument,
        buffer,
        buffer_size
    );
    if (result == (int)sizeof(struct proc_bsdinfo)
        && flavor == PROC_PIDTBSDINFO
        && is_subcommand("sandbox-manager")
        && atomic_load(&retirement_descendant_snapshotted) != 0
        && process_id == atomic_load(&retirement_descendant)) {
        struct proc_bsdinfo *info = buffer;
        info->pbi_start_tvusec ^= 1;
        if (atomic_exchange(&retirement_identity_changed, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_IDENTITY_CHANGED");
        }
    }
    return result;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
static int race_retirement_descendant_exit(pid_t process_id, int number) {
    if (number == SIGKILL
        && is_subcommand("sandbox-manager")
        && atomic_load(&retirement_descendant_snapshotted) != 0
        && process_id == atomic_load(&retirement_descendant)
        && atomic_exchange(&retirement_signal_gated, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_GATE");
        wait_for_release("MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE");
        errno = ESRCH;
        return -1;
    }
    return next_kill()(process_id, number);
}
#endif

__attribute__((constructor))
static void configure_interposer(void) {
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER) \
    || defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_IDENTITY)
    if (!is_subcommand("sandbox-manager") && !is_subcommand("sandbox")) {
        unsetenv("DYLD_INSERT_LIBRARIES");
    }
#else
    unsetenv("DYLD_INSERT_LIBRARIES");
#endif
}

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
static int gate_manager_group_cleanup(pid_t process_group_id, int number) {
    int result = next_killpg()(process_group_id, number);
    int saved_errno = errno;
    if (number == SIGKILL && is_subcommand("sandbox-manager")) {
        if (atomic_exchange(&gated_manager_group_cleanup, 1) != 0) {
            errno = EIO;
            return -1;
        }
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
        if (getenv("MCP_CONSOLE_TEST_LATE_CLEANUP") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_LATE_CLEANUP");
            wait_for_release("MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE");
        }
#else
        if (getenv("MCP_CONSOLE_TEST_RETIREMENT_CLEANUP") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_CLEANUP");
            wait_for_release("MCP_CONSOLE_TEST_RETIREMENT_RELEASE");
        }
#endif
    }
    errno = saved_errno;
    return result;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static pid_t gate_manager_start(void) {
    if (getenv("MCP_CONSOLE_TEST_MANAGER_START") != NULL
        && is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return getppid();
}

#endif

#if defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER)
static pid_t gate_manager_spawn(void) {
    int fork_index = atomic_fetch_add(&sandbox_fork_count, 1);
    if (is_subcommand("sandbox") && fork_index == 1) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_SPAWN");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_SPAWN_RELEASE");
    }
    return fork();
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE)
typedef int (*pthread_create_function)(
    pthread_t *,
    const pthread_attr_t *,
    void *(*)(void *),
    void *
);

static pthread_create_function next_pthread_create(void) {
    return pthread_create;
}

static int fail_owner_monitor_start(
    pthread_t *thread,
    const pthread_attr_t *attributes,
    void *(*start_routine)(void *),
    void *argument
) {
    if (is_subcommand("sandbox")
        && getenv("MCP_CONSOLE_TEST_OWNER_MONITOR_START_FAILURE") != NULL
        && atomic_exchange(&failed_owner_monitor_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_OWNER_MONITOR_START_FAILURE");
        wait_for_release("MCP_CONSOLE_TEST_OWNER_MONITOR_START_RELEASE");
        return EAGAIN;
    }
    return next_pthread_create()(thread, attributes, start_routine, argument);
}

static int gate_owner_manager_stop(pid_t process_id, int number) {
    if (number == SIGKILL
        && is_subcommand("sandbox")
        && atomic_load(&failed_owner_monitor_start) != 0
        && getenv("MCP_CONSOLE_TEST_OWNER_MANAGER_STOP") != NULL
        && atomic_exchange(&gated_owner_manager_stop, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_OWNER_MANAGER_STOP");
        wait_for_release("MCP_CONSOLE_TEST_OWNER_MANAGER_STOP_RELEASE");
    }
    return next_kill()(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static int observe_manager_process_watches(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    if (is_subcommand("sandbox-manager")
        && changes == NULL
        && change_count == 0
        && events != NULL
        && event_count > 0
        && timeout == NULL
        && atomic_load(&manager_observed_descendant) != 0
        && atomic_exchange(&manager_descendant_observed_reported, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED");
    }

    int result = next_kevent()(
        descriptor,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
    if (result >= 0
        && is_subcommand("sandbox-manager")
        && changes != NULL
        && change_count == 1
        && changes[0].filter == EVFILT_PROC) {
        pid_t process_id = (pid_t)changes[0].ident;
        if ((changes[0].flags & EV_DELETE) != 0
            && process_id == atomic_load(&manager_observed_descendant)) {
            atomic_store(&manager_observed_descendant, 0);
        } else if ((changes[0].flags & EV_ADD) != 0
            && (changes[0].fflags & NOTE_EXIT) != 0) {
            pid_t root = atomic_load(&manager_observed_root);
            if (root == 0) {
                atomic_store(&manager_observed_root, process_id);
            } else if (process_id != root
                && atomic_load(&manager_observed_descendant) == 0) {
                atomic_store(&manager_observed_descendant, process_id);
            }
        }
    }
    return result;
}

static int fail_manager_group_stop(pid_t process_group_id, int number) {
    if (number == SIGKILL && is_subcommand("sandbox-manager")) {
        atomic_store(&manager_group_stop_started, 1);
        if (getenv("MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE");
            errno = EPERM;
            return -1;
        }
    }
    return next_killpg()(process_group_id, number);
}

static int fail_manager_root_stop(pid_t process_id, int number) {
    if (process_id > 0
        && number == SIGKILL
        && is_subcommand("sandbox-manager")
        && atomic_load(&manager_group_stop_started) != 0) {
        pid_t root = atomic_load(&manager_observed_root);
        pid_t descendant = atomic_load(&manager_observed_descendant);
        if (descendant != 0 && process_id == descendant) {
            int result = next_kill()(process_id, number);
            if (result == 0
                && atomic_exchange(&manager_descendant_stop_reported, 1) == 0) {
                signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL");
            }
            return result;
        }
        if (root != 0 && process_id == root) {
            if (atomic_exchange(&manager_root_stop_reported, 1) == 0) {
                signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE");
            }
            errno = EPERM;
            return -1;
        }
    }
    kill_function kill_next = next_kill();
    if (kill_next == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL)
static int deny_first_sigkill(pid_t process_id, int number) {
    if (number == SIGKILL
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGKILL") != NULL
        && atomic_exchange(&denied_sigkill, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_DENIED_SIGKILL");
        errno = EPERM;
        return -1;
    }
    kill_function kill_next = next_kill();
    if (kill_next == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static int deny_first_sigkill(pid_t process_id, int number) {
    if (number == SIGKILL
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGKILL") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&denied_sigkill, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_DENIED_SIGKILL");
        errno = EPERM;
        return -1;
    }
    kill_function kill_next = next_kill();
    if (kill_next == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static pid_t gate_root_reap(pid_t process_id, int *status, int options) {
    pid_t result = next_waitpid()(process_id, status, options);
    if (result > 0
        && options == 0
        && pthread_main_np() != 0
        && getenv("MCP_CONSOLE_TEST_ROOT_REAPED") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&reaped_root, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_ROOT_REAPED");
        wait_for_release("MCP_CONSOLE_TEST_ROOT_REAP_RELEASE");
    }
    return result;
}

static int delay_late_recovery(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && atomic_load(&reaped_root) != 0
        && getenv("MCP_CONSOLE_TEST_LATE_RECOVERY") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&delayed_late_recovery, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_LATE_RECOVERY");
        wait_for_release("MCP_CONSOLE_TEST_LATE_RECOVERY_RELEASE");
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}
#endif

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

#if defined(MCP_CONSOLE_INTERPOSE_ROOT_BEFORE_MANAGER)
DYLD_INTERPOSE(gate_manager_spawn, fork)
#elif defined(MCP_CONSOLE_INTERPOSE_OWNER_MONITOR_START_FAILURE)
DYLD_INTERPOSE(fail_owner_monitor_start, pthread_create)
DYLD_INTERPOSE(gate_owner_manager_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
DYLD_INTERPOSE(gate_manager_start, getppid)
#elif defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
DYLD_INTERPOSE(observe_manager_process_watches, kevent)
DYLD_INTERPOSE(fail_manager_group_stop, killpg)
DYLD_INTERPOSE(fail_manager_root_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL)
DYLD_INTERPOSE(deny_first_sigkill, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
DYLD_INTERPOSE(fail_process_info, proc_pidinfo)
DYLD_INTERPOSE(fail_group_stop, killpg)
DYLD_INTERPOSE(gate_recovery_root_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
DYLD_INTERPOSE(arm_root_identity_recheck, kevent)
DYLD_INTERPOSE(fail_root_observer, proc_pidinfo)
DYLD_INTERPOSE(fail_root_group_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
DYLD_INTERPOSE(gate_manager_group_cleanup, killpg)
DYLD_INTERPOSE(deny_first_sigkill, kill)
DYLD_INTERPOSE(gate_root_reap, waitpid)
DYLD_INTERPOSE(delay_late_recovery, proc_pidinfo)
#elif defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
DYLD_INTERPOSE(gate_manager_group_cleanup, killpg)
#elif defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_REUSED_IDENTITY)
DYLD_INTERPOSE(observe_retirement_processes, kevent)
DYLD_INTERPOSE(observe_retirement_child_snapshot, proc_listchildpids)
DYLD_INTERPOSE(reuse_retirement_descendant_identity, proc_pidinfo)
#elif defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_EXIT_RACE)
DYLD_INTERPOSE(observe_retirement_processes, kevent)
DYLD_INTERPOSE(observe_retirement_child_snapshot, proc_listchildpids)
DYLD_INTERPOSE(race_retirement_descendant_exit, kill)
#endif
