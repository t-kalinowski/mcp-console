#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Atomic int gated_manager_start = 0;
static _Atomic int server_fork_count = 0;
static _Atomic pid_t denied_cleanup_root = 0;
static _Atomic int reported_direct_cleanup_denial = 0;

typedef int (*kill_function)(pid_t, int);
typedef int (*killpg_function)(pid_t, int);

static kill_function next_kill(void) {
    return kill;
}

static killpg_function next_killpg(void) {
    return killpg;
}

static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        _exit(125);
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor < 0) {
        _exit(125);
    }
    const char value = '1';
    ssize_t count;
    do {
        count = write(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}

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

static pid_t gate_manager_start(void) {
    if (is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return getppid();
}

static pid_t gate_manager_spawn(void) {
    int fork_index = atomic_fetch_add(&server_fork_count, 1);
    if (is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_MANAGER_SPAWN") != NULL
        && fork_index == 1) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_SPAWN");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_SPAWN_RELEASE");
    }
    return fork();
}

static int deny_startup_cleanup_group(pid_t process_group_id, int number) {
    if (number == SIGKILL
        && is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_DENY_STARTUP_CLEANUP") != NULL) {
        atomic_store(&denied_cleanup_root, process_group_id);
        errno = EIO;
        return -1;
    }
    killpg_function killpg_next = next_killpg();
    return killpg_next(process_group_id, number);
}

static int deny_startup_cleanup_process(pid_t process_id, int number) {
    if (number == SIGKILL
        && process_id == atomic_load(&denied_cleanup_root)
        && is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_DENY_STARTUP_CLEANUP") != NULL) {
        if (atomic_exchange(&reported_direct_cleanup_denial, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_DIRECT_KILL_DENIED");
        }
        errno = EPERM;
        return -1;
    }
    kill_function kill_next = next_kill();
    return kill_next(process_id, number);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(gate_manager_start, getppid)
DYLD_INTERPOSE(gate_manager_spawn, fork)
DYLD_INTERPOSE(deny_startup_cleanup_group, killpg)
DYLD_INTERPOSE(deny_startup_cleanup_process, kill)
