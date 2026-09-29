#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __linux__
#include <dlfcn.h>
static int (*next_execvp)(const char *, char *const[]);
__attribute__((constructor)) static void initialize(void) {
    next_execvp = dlsym(RTLD_NEXT, "execvp");
    if (next_execvp == NULL) _exit(125);
}
#define execvp next_execvp
#endif

/* Observe the actual frontend-to-runner payload without replacing the runner,
 * changing its environment, or loading this fixture into the workload. */
static int capture_execvp(const char *path, char *const arguments[]) {
    if (arguments[0] != NULL && arguments[1] != NULL &&
        strcmp(arguments[1], "--config-env") == 0) {
        const char *destination = getenv("MCP_CONSOLE_TEST_RUNNER_CONFIGURATION");
        const char *configuration = getenv(arguments[2]);
        if (destination == NULL || configuration == NULL) _exit(125);
        int output = open(destination, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (output < 0) _exit(125);
        size_t length = strlen(configuration);
        if (write(output, configuration, length) != (ssize_t)length ||
            write(output, "\n", 1) != 1 || close(output) != 0) _exit(125);
        const char *started_path = getenv("MCP_CONSOLE_TEST_RUNNER_STARTED");
        if (started_path != NULL) {
            int started = open(started_path, O_WRONLY);
            int release = open(getenv("MCP_CONSOLE_TEST_RUNNER_RELEASE"), O_RDONLY);
            FILE *pid = fopen(getenv("MCP_CONSOLE_TEST_RUNNER_PID"), "w");
            if (started < 0 || release < 0 || pid == NULL) _exit(125);
            if (fprintf(pid, "%ld", (long)getpid()) < 0 || fclose(pid) != 0 ||
                write(started, "1", 1) != 1) _exit(125);
            char token;
            if (read(release, &token, 1) != 1 || token != '1') _exit(125);
            close(started);
            close(release);
        }
    }
    return execvp(path, arguments);
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&capture_execvp,
    (const void *)(uintptr_t)&execvp,
};
#else
#undef execvp
int execvp(const char *path, char *const arguments[]) {
    return capture_execvp(path, arguments);
}
#endif
