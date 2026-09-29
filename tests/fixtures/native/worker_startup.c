#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* Stop the actual worker before Ready, after its launcher owns private storage. */
__attribute__((constructor)) static void gate_worker_startup(void) {
    if (getenv("MCP_CONSOLE_SIDEBAND_WRITE_FD") == NULL) return;
    FILE *state = fopen(getenv("MCP_CONSOLE_TEST_WORKER_STATE"), "w");
    if (state == NULL) _exit(125);
    if (fprintf(state, "%ld\n%s\n", (long)getpid(), getenv("TMPDIR")) < 0 ||
        fclose(state) != 0) _exit(125);
    int started = open(getenv("MCP_CONSOLE_TEST_WORKER_STARTED"), O_WRONLY);
    if (started < 0 || write(started, "1", 1) != 1) _exit(125);
    close(started);
    int release = open(getenv("MCP_CONSOLE_TEST_WORKER_RELEASE"), O_RDONLY);
    char token;
    if (release < 0 || read(release, &token, 1) != 1 || token != '1') _exit(125);
    close(release);
}
