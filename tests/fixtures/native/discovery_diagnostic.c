#include <string.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#endif

__attribute__((constructor)) static void discovery_diagnostic(int argc, char **argv) {
#ifdef __APPLE__
    argc = *_NSGetArgc();
    argv = *_NSGetArgv();
#endif
    if (argc > 1 && strcmp(argv[1], "resolve") == 0) {
        const char message[] = "preparation detail\n";
        if (write(STDERR_FILENO, message, sizeof(message) - 1) != sizeof(message) - 1)
            _exit(125);
    }
}
