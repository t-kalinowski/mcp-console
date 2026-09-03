#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

typedef int (*killpg_function)(pid_t, int);
typedef int (*kill_function)(pid_t, int);
typedef pid_t (*getpgid_function)(pid_t);
typedef ssize_t (*send_function)(int, const void *, size_t, int);

static _Atomic int reported_group_close = 0;
static _Atomic int gated_owner_release = 0;
static pid_t denied_process_group = 0;
static int added_late_member = 0;
static pid_t seed_member = 0;
static pid_t late_member = 0;

static const uint8_t MANAGER_READY = 1;
static const uint8_t TARGET_GATE_RELEASE = 1;

static pid_t add_process_group_member(pid_t process_group);

static killpg_function next_killpg(void) {
    return killpg;
}

static kill_function next_kill(void) {
    return kill;
}

static getpgid_function next_getpgid(void) {
    return getpgid;
}

static send_function next_send(void) {
    return send;
}

static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}

static int is_manager(void) {
    return is_subcommand("sandbox-manager");
}

static void checkpoint(const char *name) {
    const char *path = getenv(name);
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY);
    if (descriptor < 0) {
        return;
    }
    char byte = '1';
    write(descriptor, &byte, sizeof(byte));
    close(descriptor);
}

static void wait_for_checkpoint_release(const char *name) {
    const char *path = getenv(name);
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_RDONLY);
    char byte;
    if (descriptor >= 0) {
        read(descriptor, &byte, sizeof(byte));
        close(descriptor);
    }
}

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *path = getenv(name);
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static void write_member_marker(pid_t process_id, pid_t process_group) {
    const char *path = getenv("MCP_CONSOLE_TEST_LATE_MEMBER_MARKER");
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d %d\n", process_id, process_group);
        close(descriptor);
    }
}

static pid_t add_process_group_member(pid_t process_group) {
    int descriptors[2];
    if (pipe(descriptors) != 0) {
        return -1;
    }

    pid_t member = fork();
    if (member < 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return -1;
    }
    if (member == 0) {
        close(descriptors[0]);
        if (setpgid(0, process_group) != 0) {
            _exit(1);
        }
        pid_t process_id = getpid();
        if (write(descriptors[1], &process_id, sizeof(process_id))
            != sizeof(process_id)) {
            _exit(1);
        }
        close(descriptors[1]);
        for (;;) {
            pause();
        }
    }

    close(descriptors[1]);
    pid_t acknowledged_member = 0;
    ssize_t bytes_read;
    do {
        bytes_read = read(
            descriptors[0],
            &acknowledged_member,
            sizeof(acknowledged_member)
        );
    } while (bytes_read < 0 && errno == EINTR);
    int read_error = bytes_read < 0 ? errno : EIO;
    close(descriptors[0]);

    if (bytes_read != sizeof(acknowledged_member)
        || acknowledged_member != member) {
        next_kill()(member, SIGKILL);
        while (waitpid(member, NULL, 0) < 0 && errno == EINTR) {
        }
        errno = read_error;
        return -1;
    }
    return member;
}

static ssize_t interpose_manager_control_send(
    int socket,
    const void *buffer,
    size_t length,
    int flags
) {
    if (!is_manager()
        && (is_subcommand("serve") || is_subcommand("sandbox"))
        && length == 1
        && ((const uint8_t *)buffer)[0] == TARGET_GATE_RELEASE
        && getenv("MCP_CONSOLE_TEST_OWNER_GATE_READY") != NULL
        && atomic_exchange(&gated_owner_release, 1) == 0) {
        checkpoint("MCP_CONSOLE_TEST_OWNER_GATE_READY");
        wait_for_checkpoint_release("MCP_CONSOLE_TEST_OWNER_GATE_RELEASE");
    }
    send_function send_next = next_send();
    ssize_t sent = send_next(socket, buffer, length, flags);
    if (is_manager()
        && socket == STDIN_FILENO
        && sent == (ssize_t)length
        && length == 1
        && ((const uint8_t *)buffer)[0] == MANAGER_READY) {
        checkpoint("MCP_CONSOLE_TEST_MANAGER_READY_SENT");
        wait_for_checkpoint_release("MCP_CONSOLE_TEST_MANAGER_READY_RETURN");
    }
    return sent;
}

static int manager_group_close(pid_t process_group, int number) {
    if (number == SIGKILL && is_manager()) {
        if (atomic_exchange(&reported_group_close, 1) == 0) {
            checkpoint("MCP_CONSOLE_TEST_MANAGER_GROUP_CLOSED");
        }
        if (getenv("MCP_CONSOLE_TEST_KILLPG_MARKER") != NULL) {
            denied_process_group = process_group;
            // Keep one manager child in the first exact-group snapshot. Its
            // membership check adds another child after that snapshot.
            seed_member = add_process_group_member(process_group);
            if (seed_member < 0) {
                return -1;
            }
            write_pid_marker("MCP_CONSOLE_TEST_KILLPG_MARKER", process_group);
            errno = EPERM;
            return -1;
        }
    }
    killpg_function killpg_next = next_killpg();
    return killpg_next(process_group, number);
}

static pid_t getpgid_and_add_member(pid_t process_id) {
    pid_t process_group = next_getpgid()(process_id);
    // Rust rechecks group membership only after taking its kernel snapshot.
    // Join the group here so a one-pass fallback cannot observe this child.
    if (process_group == denied_process_group && !added_late_member) {
        added_late_member = 1;
        pid_t member = add_process_group_member(process_group);
        if (member < 0) {
            return -1;
        }
        late_member = member;
        write_member_marker(member, process_group);
    }
    return process_group;
}

static int kill_and_reap_added_member(pid_t process_id, int number) {
    int result = next_kill()(process_id, number);
    int signal_error = errno;
    if (result == 0 && number == SIGKILL
        && (process_id == seed_member || process_id == late_member)) {
        int status = 0;
        pid_t waited;
        do {
            waited = waitpid(process_id, &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != process_id) {
            return -1;
        }
        if (process_id == late_member) {
            write_pid_marker("MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER", process_id);
            late_member = 0;
        } else {
            seed_member = 0;
        }
    }
    errno = signal_error;
    return result;
}

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&interpose_manager_control_send, (const void *)&send},
    {(const void *)&manager_group_close, (const void *)&killpg},
    {(const void *)&getpgid_and_add_member, (const void *)&getpgid},
    {(const void *)&kill_and_reap_added_member, (const void *)&kill},
};
