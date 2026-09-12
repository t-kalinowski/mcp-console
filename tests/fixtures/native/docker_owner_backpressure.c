#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <stdbool.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#endif

static bool is_docker_owner(void) {
#ifdef __APPLE__
    return *_NSGetArgc() > 1 && strcmp((*_NSGetArgv())[1], "docker-owner") == 0;
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) return false;
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    return next < (size_t)count && strcmp(args + next, "docker-owner") == 0;
#endif
}

#define MCP_CONSOLE_BACKPRESSURE_SELECT is_docker_owner
#include "relay_stdout_backpressure.c"
