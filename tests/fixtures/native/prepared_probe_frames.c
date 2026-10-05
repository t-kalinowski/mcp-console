#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
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
static int (*native_poll)(struct pollfd *, nfds_t, int);
#define write native_write
#define poll native_poll
#endif

// Receipts concern the owner -> controller pipe, never the attachment pipe.
static bool selected;
static struct stat output_identity;
static int header_ready, release_payload, exit_handled, abort_gate, receipts;
static _Thread_local int cancellation_reader = -1;
static _Thread_local int owner_cancellation = -1, output_completion = -1;
static _Thread_local bool attachment_exited;
static _Thread_local unsigned char header[5];
static _Thread_local size_t header_bytes;
static _Thread_local uint32_t payload_length, payload_bytes;
static _Thread_local bool hello_gated;

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

static int open_checkpoint(const char *root, const char *name, int flags) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(90);
    int fd = open(path, flags | O_CLOEXEC);
    if (fd < 0) _exit(91);
    return fd;
}

static void notify(int fd) {
    if (write(fd, "1", 1) != 1) _exit(92);
}

static void record(const char *stage, short events) {
    char line[256];
    int length = snprintf(line, sizeof(line),
        "{\"stage\":\"%s\",\"tag\":%u,\"header_bytes\":%zu,"
        "\"length\":%u,\"payload_bytes\":%u,\"cancel_revents\":%d}\n",
        stage, header[0], header_bytes, payload_length, payload_bytes, events);
    if (length < 0 || length >= (int)sizeof(line) ||
        write(receipts, line, (size_t)length) != length) _exit(93);
}

static void gate_payload(void) {
    if (cancellation_reader < 0) _exit(94);
    notify(header_ready);
    struct pollfd waits[] = {
        {.fd = release_payload, .events = POLLIN},
        {.fd = abort_gate, .events = POLLIN},
        {.fd = cancellation_reader, .events = POLLIN},
    };
    for (;;) {
        int result;
        do { result = poll(waits, 3, -1); } while (result < 0 && errno == EINTR);
        if (result <= 0) _exit(95);
        // This is readiness on the real transfer cancellation reader. Do not
        // consume it or change the subsequent production poll's result.
        if (waits[2].revents != 0) {
            record("cancellation-ready", waits[2].revents);
            notify(exit_handled);
            waits[2].fd = -1;
        }
        // Test cancellation releases this gate without fabricating cancellation
        // on the production descriptor. Leave the abort token for the peer.
        if (waits[1].revents & POLLIN) return;
        if (waits[0].revents & POLLIN) {
            char byte;
            if (read(release_payload, &byte, 1) != 1 || byte != '1') _exit(96);
            return;
        }
    }
}

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    native_poll = dlsym(RTLD_NEXT, "poll");
    if (!native_write || !native_poll) _exit(90);
#endif
    const char *root = getenv("MCP_CONSOLE_TEST_PREPARED_PROBE");
    if (!root || !is_owner()) return;
    selected = true;
    // The attachment and provider commands must retain their ordinary I/O.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    header_ready = open_checkpoint(root, "owner-header", O_RDWR);
    release_payload = open_checkpoint(root, "owner-release", O_RDWR);
    exit_handled = open_checkpoint(root, "owner-exit-handled", O_RDWR);
    abort_gate = open_checkpoint(root, "abort", O_RDWR);
    receipts = open_checkpoint(root, "owner-frames", O_WRONLY | O_APPEND);
    if (fstat(STDOUT_FILENO, &output_identity) < 0) _exit(94);
}

static int observed_poll(struct pollfd *fds, nfds_t count, int timeout) {
    // The owner has observed attachment exit and now waits for output completion.
    // Notify before the real wait so the test can release the HELLO payload.
    // The old runtime instead reaches real cancellation readiness in the gate.
    if (selected && attachment_exited && count == 2 &&
        fds[0].fd == owner_cancellation && fds[1].fd == output_completion) {
        attachment_exited = false;
        record("draining-output", 0);
        notify(exit_handled);
    }
    int result = poll(fds, count, timeout);
    int error = errno;
    if (selected && count == 3 && result > 0 && fds[2].revents != 0 &&
        fds[0].events == POLLIN && fds[1].events == POLLIN && fds[2].events == POLLIN) {
        owner_cancellation = fds[0].fd;
        output_completion = fds[1].fd;
        attachment_exited = true;
    }
    // Io::write checks its real cancellation pipe before every write. Remember
    // that reader on the writing thread, not an unrelated owner/input watcher.
    if (selected && count == 1 && timeout == 0 && fds[0].events == POLLIN) {
        cancellation_reader = fds[0].fd;
        if (hello_gated && result > 0 && fds[0].revents != 0) {
            record("forwarding-cancelled", fds[0].revents);
        }
    }
    errno = error;
    return result;
}

static ssize_t observed_write(int fd, const void *buffer, size_t length) {
    ssize_t result = write(fd, buffer, length);
    int error = errno;
    struct stat identity;
    if (!selected || result <= 0 || fstat(fd, &identity) < 0 ||
        identity.st_dev != output_identity.st_dev ||
        identity.st_ino != output_identity.st_ino) {
        errno = error;
        return result;
    }
    const unsigned char *bytes = buffer;
    size_t accepted = (size_t)result;
    bool gate = false;
    while (accepted > 0) {
        if (header_bytes < sizeof(header)) {
            header[header_bytes++] = *bytes++;
            --accepted;
            if (header_bytes == sizeof(header)) {
                payload_length = ((uint32_t)header[1] << 24) |
                    ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 8) | header[4];
                payload_bytes = 0;
                if (header[0] == 1 && !hello_gated) gate = true;
            }
        } else {
            uint32_t remaining = payload_length - payload_bytes;
            size_t count = accepted < remaining ? accepted : remaining;
            payload_bytes += (uint32_t)count;
            bytes += count;
            accepted -= count;
        }
        if (header_bytes == sizeof(header) && payload_bytes == payload_length) {
            record("frame-complete", 0);
            header_bytes = 0;
        }
    }
    record("write-accepted", 0);
    if (gate) {
        hello_gated = true;
        // Hold only after bytes were accepted by the actual owner stdout write.
        // If a writer emits a whole frame in one syscall, its payload is already
        // complete; do not split that syscall or remove any accepted bytes.
        gate_payload();
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
INTERPOSE(observed_poll, poll)
#else
#undef write
#undef poll
ssize_t write(int fd, const void *buffer, size_t length) {
    return observed_write(fd, buffer, length);
}
int poll(struct pollfd *fds, nfds_t count, int timeout) {
    return observed_poll(fds, count, timeout);
}
#endif
