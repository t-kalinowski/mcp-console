#define _GNU_SOURCE

#include <fcntl.h>
#include <pthread.h>
#include <stdlib.h>
#include <unistd.h>

static const int inherited_descriptor = 211;
static const char *marker_path = NULL;

static void open_inherited_descriptor(void) {
  int descriptor = open(marker_path, O_WRONLY | O_APPEND | O_CREAT, 0600);
  if (descriptor < 0) {
    _exit(91);
  }
  if (dup2(descriptor, inherited_descriptor) < 0) {
    _exit(92);
  }
  if (descriptor != inherited_descriptor) {
    close(descriptor);
  }
}

__attribute__((constructor)) static void register_at_fork_callback(void) {
  marker_path = getenv("MCP_CONSOLE_TEST_AT_FORK_FD_PATH");
  if (marker_path != NULL && pthread_atfork(open_inherited_descriptor, NULL, NULL) != 0) {
    _exit(93);
  }
}
