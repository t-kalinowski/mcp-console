#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static atomic_int observed_pid;
static atomic_bool settled;
static atomic_bool interrupted;

__attribute__((constructor)) static void initialize(void) {
    // Interpose only the relay or preparation owner, never its child.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static void checkpoint(const char *name, int flags) {
    int descriptor = open(getenv(name), flags);
    char token = '1';
    if (descriptor < 0) _exit(120);
    ssize_t count = flags == O_RDONLY ? read(descriptor, &token, 1)
                                     : write(descriptor, &token, 1);
    if (count != 1 || token != '1') _exit(121);
    close(descriptor);
}

static int observe_exit(idtype_t type, id_t id, siginfo_t *info, int options) {
    bool blocking = type == P_PID && (options & WNOWAIT) && !(options & WNOHANG);
    if (blocking && atomic_load(&observed_pid) == 0) {
        atomic_store(&observed_pid, (int)id);
        checkpoint("MCP_CONSOLE_TEST_OBSERVER_ENTERED", O_WRONLY);
        checkpoint("MCP_CONSOLE_TEST_OBSERVER_RELEASE", O_RDONLY);
    }
    if (blocking && getenv("MCP_CONSOLE_TEST_OBSERVER_FAIL") != NULL) {
        atomic_store(&settled, true);
        errno = EIO;
        return -1;
    }
    if (blocking && getenv("MCP_CONSOLE_TEST_OBSERVER_EINTR") != NULL &&
        !atomic_exchange(&interrupted, true)) {
        errno = EINTR;
        return -1;
    }
#ifdef __APPLE__
    int result = waitid(type, id, info, options);
#else
    int result = ((int (*)(idtype_t, id_t, siginfo_t *, int))dlsym(RTLD_NEXT, "waitid"))(
        type, id, info, options);
#endif
    int saved_errno = errno;
    if (blocking && result == 0 &&
        (info->si_code == CLD_EXITED || info->si_code == CLD_KILLED || info->si_code == CLD_DUMPED)) {
        atomic_store(&settled, true);
    }
    errno = saved_errno;
    return result;
}

static pid_t observe_reap(pid_t pid, int *status, int options) {
    if (pid == atomic_load(&observed_pid) && !atomic_load(&settled)) {
        // Even a WNOHANG try_wait can reap and release the child's PID.
        int descriptor = open(getenv("MCP_CONSOLE_TEST_EARLY_REAP"),
                              O_WRONLY | O_CREAT, 0600);
        if (descriptor < 0) _exit(122);
        close(descriptor);
    }
#ifdef __APPLE__
    return waitpid(pid, status, options);
#else
    return ((pid_t (*)(pid_t, int *, int))dlsym(RTLD_NEXT, "waitpid"))(pid, status, options);
#endif
}

static int observe_kill(pid_t pid, int signal) {
#ifdef __APPLE__
    int result = kill(pid, signal);
#else
    int result = ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, signal);
#endif
    int saved_errno = errno;
    if (signal == SIGKILL && (pid == atomic_load(&observed_pid) ||
                             -pid == atomic_load(&observed_pid))) {
        checkpoint("MCP_CONSOLE_TEST_CHILD_KILLED", O_WRONLY);
    }
    errno = saved_errno;
    return result;
}

static int observe_killpg(pid_t group, int signal) {
    return observe_kill(-group, signal);
}

#ifdef __APPLE__
#define INTERPOSE(replacement, replacee)                                     \
    __attribute__((used)) static struct {                                   \
        const void *replacement;                                           \
        const void *replacee;                                               \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {\
        (const void *)&replacement, (const void *)&replacee,                 \
    };
INTERPOSE(observe_exit, waitid)
INTERPOSE(observe_reap, waitpid)
INTERPOSE(observe_kill, kill)
INTERPOSE(observe_killpg, killpg)
#else
int waitid(idtype_t type, id_t id, siginfo_t *info, int options) {
    return observe_exit(type, id, info, options);
}
pid_t waitpid(pid_t pid, int *status, int options) { return observe_reap(pid, status, options); }
int kill(pid_t pid, int signal) { return observe_kill(pid, signal); }
int killpg(pid_t group, int signal) { return observe_killpg(group, signal); }
#endif
