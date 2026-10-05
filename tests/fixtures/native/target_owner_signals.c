#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#else
#include <dlfcn.h>
static ssize_t (*native_write)(int, const void *, size_t);
static int (*native_sigaction)(int, const struct sigaction *, struct sigaction *);
#define write native_write
#define sigaction(...) native_sigaction(__VA_ARGS__)
#endif

static bool selected;
static atomic_bool filled;
static bool blocked;
static _Thread_local bool in_handler;
static void (*handlers[NSIG])(int);
static int full, returned, installed, release, retirement, output_blocked;
static int input_closed;
static int cancelled;
static bool installation_gate;
static bool controller_closure;
static bool interrupted_write;
static atomic_bool interrupt_once;
static struct stat output_identity;

static bool is_owner(void) {
#ifdef __APPLE__
    if (*_NSGetArgc() <= 1) return false;
    const char *operation = (*_NSGetArgv())[1];
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) return false;
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    if (next >= (size_t)count) return false;
    const char *operation = args + next;
#endif
    return strcmp(operation, "docker-sandbox-owner") == 0;
}

static int checkpoint(const char *root, const char *name) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(90);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) _exit(91);
    return fd;
}

static void notify(int fd) {
    if (write(fd, "1", 1) != 1) _exit(92);
}

static short wait_checkpoint(int fd, short events) {
    struct pollfd waits[] = {
        {.fd = fd, .events = events},
        {.fd = cancelled, .events = POLLIN},
    };
    int ready;
    do { ready = poll(waits, 2, -1); } while (ready < 0 && errno == EINTR);
    if (ready <= 0) _exit(93);
    // Leave cancellation unread so it also releases later gates.
    if (waits[1].revents & POLLIN) return 0;
    return waits[0].revents;
}

static void gate(void) {
    if (!wait_checkpoint(release, POLLIN)) return;
    char byte;
    ssize_t result;
    do { result = read(release, &byte, 1); } while (result < 0 && errno == EINTR);
    if (result != 1 || byte != '1') _exit(93);
}

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    native_sigaction = dlsym(RTLD_NEXT, "sigaction");
    if (!native_write || !native_sigaction) _exit(90);
#endif
    const char *root = getenv("MCP_CONSOLE_TEST_OWNER_SIGNALS");
    if (!root || !is_owner()) return;
    selected = true;
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    full = checkpoint(root, "full");
    returned = checkpoint(root, "returned");
    installed = checkpoint(root, "installed");
    release = checkpoint(root, "release");
    retirement = checkpoint(root, "retirement");
    output_blocked = checkpoint(root, "blocked");
    input_closed = checkpoint(root, "input-closed");
    cancelled = checkpoint(root, "cancel");
    installation_gate = getenv("MCP_CONSOLE_TEST_SIGNAL_INSTALLATION") != NULL;
    controller_closure = getenv("MCP_CONSOLE_TEST_SIGNAL_CONTROLLER_CLOSE") != NULL;
    interrupted_write = getenv("MCP_CONSOLE_TEST_SIGNAL_EINTR") != NULL;
    interrupt_once = interrupted_write;
    if (fstat(STDOUT_FILENO, &output_identity) < 0) _exit(94);
    char path[4096];
    snprintf(path, sizeof(path), "%s/pid", root);
    FILE *pid = fopen(path, "w");
    if (!pid) _exit(94);
    fprintf(pid, "%d", getpid());
    fclose(pid);
}

static void observe_signal(int signal) {
    int saved_errno = errno;
    in_handler = true;
    errno = EDOM;
    handlers[signal](signal);
    // Verify the delegated handler preserves errno, including EAGAIN publication.
    if (errno != EDOM) _exit(95);
    in_handler = false;
    notify(returned);
    errno = saved_errno;
}

static int observed_sigaction(int signal, const struct sigaction *action, struct sigaction *old) {
    if (!selected || !action || action->sa_handler == SIG_DFL || action->sa_handler == SIG_IGN ||
        (signal != SIGTERM && signal != SIGINT && signal != SIGHUP)) {
        return sigaction(signal, action, old);
    }
    handlers[signal] = action->sa_handler;
    struct sigaction wrapped = *action;
    wrapped.sa_handler = observe_signal;
    int result = sigaction(signal, &wrapped, old);
    if (result == 0 && signal == SIGHUP && installation_gate) {
        notify(installed);
        gate();
    }
    return result;
}

static ssize_t observed_write(int fd, const void *buffer, size_t length) {
    if (in_handler && interrupt_once) {
        interrupt_once = false;
        errno = EINTR;
        return -1;
    }
    if (in_handler && !filled && !interrupted_write) {
        // Fill the actual wakeup pipe before its first publication, then restore
        // the production flags. A blocking handler cannot pass this checkpoint.
        int flags = fcntl(fd, F_GETFL);
        if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) _exit(96);
        char bytes[4096] = {0};
        while (write(fd, bytes, sizeof(bytes)) >= 0) {}
        // Fill any remaining capacity with one-byte notifications.
        if (errno != EAGAIN) _exit(97);
        while (write(fd, "1", 1) == 1) {}
        if (errno != EAGAIN || fcntl(fd, F_SETFL, flags) < 0) _exit(97);
        filled = true;
        notify(full);
    }
    if (in_handler || !selected) return write(fd, buffer, length);
    struct stat identity;
    if (fstat(fd, &identity) < 0 || identity.st_dev != output_identity.st_dev ||
        identity.st_ino != output_identity.st_ino) return write(fd, buffer, length);
    if ((filled || interrupted_write) && length == 1 && *(const unsigned char *)buffer == 3) {
        // Hold the one retirement frame after provider cleanup, while handlers
        // remain installed and the ordinary signal watcher has already exited.
        notify(retirement);
        if (controller_closure) {
            // The controller closes owner stdin after observing cancellation.
            // Wait for that boundary before allowing owner exit.
            short events = wait_checkpoint(STDIN_FILENO, POLLIN);
            if (events) {
                if (!(events & POLLHUP)) _exit(98);
                notify(input_closed);
            }
        }
        gate();
    }
    ssize_t result = write(fd, buffer, length);
    int error = errno;
    if (result < 0 && error == EAGAIN && !blocked) {
        blocked = true;
        notify(output_blocked);
    }
    errno = error;
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \
    interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&replacee };
INTERPOSE(observed_write, write)
INTERPOSE(observed_sigaction, sigaction)
#else
#undef write
#undef sigaction
ssize_t write(int fd, const void *buffer, size_t length) { return observed_write(fd, buffer, length); }
int sigaction(int signal, const struct sigaction *action, struct sigaction *old) {
    return observed_sigaction(signal, action, old);
}
#endif
