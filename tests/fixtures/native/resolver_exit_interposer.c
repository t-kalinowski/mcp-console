#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t owner;
static atomic_bool claimed = 0;

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static int observe_exit(idtype_t type, id_t id, siginfo_t *info, int options) {
#ifdef __APPLE__
    int result = waitid(type, id, info, options);
#else
    int result = ((int (*)(idtype_t, id_t, siginfo_t *, int))
                  dlsym(RTLD_NEXT, "waitid"))(type, id, info, options);
#endif
    if (getpid() != owner || result != 0 || type != P_PID ||
        (options & WNOHANG) || info->si_pid == 0) return result;
    FILE *file = fopen(getenv("MCP_CONSOLE_TEST_IR_PID"), "r");
    if (file == NULL) return result;
    int selected = 0;
    int found = fscanf(file, "%d", &selected);
    fclose(file);
    if (found != 1 || selected != (int)id || atomic_exchange(&claimed, 1))
        return result;
    int started = open(getenv("MCP_CONSOLE_TEST_EXIT_STARTED"), O_WRONLY);
    int release = open(getenv("MCP_CONSOLE_TEST_EXIT_RELEASE"), O_RDONLY);
    if (started < 0 || release < 0 || write(started, "1", 1) != 1) _exit(121);
    char token;
    ssize_t count;
    do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(122);
    close(started);
    close(release);
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} wait_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observe_exit,
    (const void *)(uintptr_t)&waitid,
};
#else
int waitid(idtype_t type, id_t id, siginfo_t *info, int options) {
    return observe_exit(type, id, info, options);
}
#endif
