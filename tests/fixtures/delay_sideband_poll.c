#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static atomic_bool claimed = false;

typedef int (*poll_function)(struct pollfd *, nfds_t, int);

static poll_function next_poll(void) {
  return poll;
}

static bool target_process(void) {
  const char *value = getenv("MCP_CONSOLE_TEST_POLL_PID");
  if (value == NULL) {
    return false;
  }
  char *end = NULL;
  long process_id = strtol(value, &end, 10);
  return end != value && *end == '\0' && process_id == getpid();
}

static bool armed(void) {
  const char *path = getenv("MCP_CONSOLE_TEST_POLL_ARM");
  return path != NULL && access(path, F_OK) == 0;
}

static bool unix_stream(int descriptor) {
  struct sockaddr_un address;
  socklen_t address_length = sizeof(address);
  int type = 0;
  socklen_t type_length = sizeof(type);
  return getsockname(descriptor, (struct sockaddr *)&address, &address_length) == 0 &&
         address.sun_family == AF_UNIX &&
         getsockopt(descriptor, SOL_SOCKET, SO_TYPE, &type, &type_length) == 0 &&
         type == SOCK_STREAM;
}

static void mark(const char *name) {
  const char *path = getenv(name);
  if (path == NULL) {
    return;
  }
  int descriptor = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
  if (descriptor >= 0) {
    close(descriptor);
  }
}

static int delayed_poll(struct pollfd *descriptors, nfds_t count, int timeout) {
  poll_function poll_next = next_poll();
  if (poll_next == NULL) {
    errno = ENOSYS;
    return -1;
  }
  int result = poll_next(descriptors, count, timeout);
  if (result <= 0 || !target_process() || count != 2 || timeout != -1 ||
      !armed() || (descriptors[0].revents & POLLIN) == 0 ||
      !unix_stream(descriptors[0].fd) || atomic_exchange(&claimed, true)) {
    return result;
  }

  mark("MCP_CONSOLE_TEST_POLL_SOCKET_READY");
  if (descriptors[1].revents == 0) {
    struct pollfd cancellation = descriptors[1];
    do {
      cancellation.revents = 0;
      result = poll_next(&cancellation, 1, -1);
    } while (result < 0 && errno == EINTR);
    if (result < 0) {
      return result;
    }
    descriptors[1].revents = cancellation.revents;
  }
  mark("MCP_CONSOLE_TEST_POLL_CANCEL_READY");
  return (descriptors[0].revents != 0) + (descriptors[1].revents != 0);
}

__attribute__((constructor)) static void prevent_worker_injection(void) {
  if (target_process()) {
    mark("MCP_CONSOLE_TEST_POLL_LOADED");
    unsetenv("DYLD_INSERT_LIBRARIES");
  }
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
  __attribute__((used)) static struct {                                        \
    const void *replacement;                                                   \
    const void *replacee;                                                      \
  } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {    \
      (const void *)(uintptr_t)&replacement,                                   \
      (const void *)(uintptr_t)&replacee};

DYLD_INTERPOSE(delayed_poll, poll)
