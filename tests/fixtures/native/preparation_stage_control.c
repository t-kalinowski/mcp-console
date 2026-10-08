#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdatomic.h>
#include <sys/wait.h>
#include <unistd.h>

static atomic_int first_stage;
static atomic_int observed_later_stage;

static int after_stage_exit(pid_t group, int signal) {
    if (signal == SIGINT) {
        int unset = 0;
        atomic_compare_exchange_strong(&first_stage, &unset, group);
    }
    if (signal == SIGINT && group != atomic_load(&first_stage) &&
        atomic_exchange(&observed_later_stage, 1) == 0) {
        // The first stage consumes an interrupt and returns a valid uv path.
        // Its pending control also targets the next stage. Observe that child's
        // independent exit before forwarding control, preserving real status,
        // the owner's non-reaping observer and all process/I/O retirement.
        // Release trusted launch suspension so this fixture exercises a stage
        // failure before control rather than cancellation before execution.
        killpg(group, SIGCONT);
        siginfo_t status;
        int result;
        do {
            result = waitid(P_PID, (id_t)group, &status, WEXITED | WNOWAIT);
        } while (result < 0 && errno == EINTR);
        if (result != 0 || status.si_code != CLD_EXITED || status.si_status != 23)
            _exit(125);
    }
#ifdef __APPLE__
    return killpg(group, signal);
#else
    return ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "killpg"))(group, signal);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} killpg_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)after_stage_exit, (const void *)killpg,
};
#else
int killpg(pid_t group, int signal) { return after_stage_exit(group, signal); }
#endif
