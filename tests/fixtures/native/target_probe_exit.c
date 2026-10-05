#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <signal.h>
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
#define write native_write
#endif

static const char *mode;
static struct stat output_identity;
static int reached, release, cancelled;
static _Thread_local unsigned char header[5];
static _Thread_local size_t header_bytes;
static _Thread_local uint32_t payload_length, payload_bytes;

static bool is_owner(void) {
#ifdef __APPLE__
    return *_NSGetArgc() > 1 && strcmp((*_NSGetArgv())[1], "docker-sandbox-owner") == 0;
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) return false;
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    return next < (size_t)count && strcmp(args + next, "docker-sandbox-owner") == 0;
#endif
}

static int checkpoint(const char *root, const char *name) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", root, name) >= (int)sizeof(path)) _exit(90);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) _exit(91);
    return fd;
}

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    if (!native_write) _exit(90);
#endif
    const char *root = getenv("MCP_CONSOLE_TEST_TARGET_SETUP");
    if (!root || !is_owner()) return;
    mode = getenv("MCP_CONSOLE_TEST_TARGET_FAULT");
    if (!mode) _exit(90);
    // Provider commands and attachments retain their real cleanup behavior.
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    reached = checkpoint(root, "native-reached");
    release = checkpoint(root, "native-release");
    cancelled = checkpoint(root, "native-cancel");
    if (fstat(STDOUT_FILENO, &output_identity) < 0) _exit(91);
}

static void fail_after_receipt(void) {
    if (write(reached, "1", 1) != 1) _exit(92);
    struct pollfd waits[] = {
        {.fd = release, .events = POLLIN},
        {.fd = cancelled, .events = POLLIN},
    };
    int result;
    do { result = poll(waits, 2, -1); } while (result < 0 && errno == EINTR);
    if (result <= 0) _exit(93);
    if (waits[1].revents) return;
    char byte;
    if (read(release, &byte, 1) != 1 || byte != '1') _exit(94);
    if (strcmp(mode, "receipt-sigkill") == 0) {
        if (kill(getpid(), SIGKILL) < 0) _exit(95);
    }
    _exit(47);
}

static ssize_t observed_write(int fd, const void *buffer, size_t length) {
    ssize_t result = write(fd, buffer, length);
    int error = errno;
    struct stat identity;
    if (mode && result > 0 && fstat(fd, &identity) == 0 &&
        identity.st_dev == output_identity.st_dev && identity.st_ino == output_identity.st_ino) {
        const unsigned char *bytes = buffer;
        size_t accepted = (size_t)result;
        while (accepted > 0) {
            if (header_bytes < sizeof(header)) {
                header[header_bytes++] = *bytes++;
                --accepted;
                if (header_bytes == sizeof(header)) {
                    payload_length = ((uint32_t)header[1] << 24) |
                        ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 8) | header[4];
                    payload_bytes = 0;
                }
            } else {
                uint32_t remaining = payload_length - payload_bytes;
                size_t count = accepted < remaining ? accepted : remaining;
                payload_bytes += (uint32_t)count;
                bytes += count;
                accepted -= count;
            }
            if (header_bytes == sizeof(header) && payload_bytes == payload_length) {
                // The actual owner pipe accepted the entire terminal frame.
                // Never fabricate a receipt or fail before its payload write.
                if (header[0] == 3) fail_after_receipt();
                header_bytes = 0;
            }
        }
    }
    errno = error;
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *replacee; }
    interpose_write __attribute__((section("__DATA,__interpose"))) = {
        (const void *)(uintptr_t)&observed_write, (const void *)(uintptr_t)&write };
#else
#undef write
ssize_t write(int fd, const void *buffer, size_t length) { return observed_write(fd, buffer, length); }
#endif
