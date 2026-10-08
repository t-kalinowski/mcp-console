#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#endif

__attribute__((constructor)) static void discovery_diagnostic(int argc, char **argv) {
#ifdef __APPLE__
    argc = *_NSGetArgc();
    argv = *_NSGetArgv();
#endif
    // Worker-policy inspection clears this selection and is a separate peer.
    if (argc > 1 && strcmp(argv[1], "resolve") == 0 &&
        getenv("RETICULATE_PYTHON") != NULL) {
        const char message[] = "preparation detail\n";
        if (write(STDERR_FILENO, message, sizeof(message) - 1) != sizeof(message) - 1)
            _exit(125);
    }
}
