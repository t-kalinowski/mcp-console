#include <crt_externs.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

#include "manager_start_interposer.c"

static _Atomic int observed_setup_write = 0;

static ssize_t observe_setup_write(int descriptor, const void *buffer, size_t count) {
    const unsigned char *bytes = buffer;
    if (is_subcommand("sandbox") && count > 4 && bytes[4] == '{'
        && (((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16)
            | ((uint32_t)bytes[2] << 8) | bytes[3]) == count - 4
        && atomic_exchange(&observed_setup_write, 1) == 0) {
        // Put part of the actual frame into the setup pipe before cancellation.
        // The stopped runner cannot drain it while the remaining frame is sent.
        ssize_t prefix = write(descriptor, buffer, 1);
        if (prefix != 1) {
            _exit(125);
        }
        signal_checkpoint("MCP_CONSOLE_TEST_SETUP_WRITE_STARTED");
        wait_for_release("MCP_CONSOLE_TEST_SETUP_WRITE_RELEASE");
        ssize_t written = write(descriptor, bytes + 1, count - 1);
        return written < 0 ? prefix : prefix + written;
    }
    return write(descriptor, buffer, count);
}

DYLD_INTERPOSE(observe_setup_write, write)
