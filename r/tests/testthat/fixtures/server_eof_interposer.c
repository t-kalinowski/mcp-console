#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

__attribute__((constructor)) static void keep_injection_in_server(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

// Stall only after the wrapper closes its server input. The worker, launcher,
// and manager are still active, so releasing the public tool must eventually
// stop the server without killing the processes responsible for its cleanup.
static ssize_t hold_server_eof(int descriptor, void *buffer, size_t length) {
    ssize_t result = read(descriptor, buffer, length);
    if (descriptor != STDIN_FILENO || result != 0) {
        return result;
    }
    int marker = open(getenv("MCP_CONSOLE_TEST_SERVER_EOF"), O_WRONLY | O_CREAT, 0600);
    if (marker < 0 || write(marker, "1", 1) != 1) {
        _exit(91);
    }
    close(marker);
    int gate[2];
    if (pipe(gate) != 0) {
        _exit(92);
    }
    char token;
    while (read(gate[0], &token, 1) < 0 && errno == EINTR) {
    }
    _exit(93);
}

__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_read __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&hold_server_eof,
    (const void *)(uintptr_t)&read,
};
