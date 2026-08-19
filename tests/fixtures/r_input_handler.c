#include <R.h>
#include <R_ext/Rdynload.h>
#include <R_ext/eventloop.h>
#include <Rinternals.h>
#include <Rinterface.h>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

static InputHandler *registered_handler = NULL;
static int registered_fd = -1;
static SEXP registered_callback = NULL;
static struct sigaction saved_sigint_action;
static int sigint_action_saved = 0;

static void handle_input(void *user_data) {
  (void)user_data;

  int fd = registered_fd;
  InputHandler *handler = registered_handler;
  registered_fd = -1;
  registered_handler = NULL;
  removeInputHandler(&R_InputHandlers, handler);

  char byte;
  ssize_t length = read(fd, &byte, 1);
  close(fd);
  if (length != 1) {
    Rf_error("failed to read test input handler byte");
  }

  SEXP callback = PROTECT(registered_callback);
  registered_callback = NULL;
  R_ReleaseObject(callback);
  SEXP call = PROTECT(Rf_lang1(callback));
  Rf_eval(call, R_GlobalEnv);
  UNPROTECT(2);
}

static SEXP register_input_handler(SEXP path, SEXP callback) {
  if (registered_handler != NULL) {
    Rf_error("test input handler is already registered");
  }

  const char *fifo = CHAR(STRING_ELT(path, 0));
  if (mkfifo(fifo, S_IRUSR | S_IWUSR) < 0) {
    Rf_error("failed to create test input handler FIFO");
  }

  int fd = open(fifo, O_RDONLY | O_NONBLOCK);
  if (fd < 0) {
    Rf_error("failed to open test input handler FIFO");
  }

  InputHandler *handler =
      addInputHandler(R_InputHandlers, fd, handle_input, XActivity);
  if (handler == NULL) {
    close(fd);
    Rf_error("failed to register test input handler");
  }
  registered_fd = fd;
  registered_handler = handler;
  registered_callback = callback;
  R_PreserveObject(registered_callback);
  return R_NilValue;
}

static SEXP read_console_once(SEXP prompt) {
  unsigned char buffer[4096];
  int status = R_ReadConsole(
      CHAR(STRING_ELT(prompt, 0)),
      buffer,
      sizeof(buffer),
      0
  );
  if (status <= 0) {
    return R_NilValue;
  }
  return Rf_mkString((const char *)buffer);
}

static SEXP ignore_sigint(void) {
  if (sigint_action_saved) {
    Rf_error("test SIGINT action is already saved");
  }
  if (sigaction(SIGINT, NULL, &saved_sigint_action) < 0) {
    Rf_error("failed to save test SIGINT action");
  }

  struct sigaction ignored = {0};
  ignored.sa_handler = SIG_IGN;
  sigemptyset(&ignored.sa_mask);
  if (sigaction(SIGINT, &ignored, NULL) < 0) {
    Rf_error("failed to ignore SIGINT");
  }
  sigint_action_saved = 1;
  return R_NilValue;
}

static SEXP restore_sigint(void) {
  if (!sigint_action_saved) {
    Rf_error("test SIGINT action is not saved");
  }
  if (sigaction(SIGINT, &saved_sigint_action, NULL) < 0) {
    Rf_error("failed to restore test SIGINT action");
  }
  sigint_action_saved = 0;
  return R_NilValue;
}

static const R_CallMethodDef call_methods[] = {
    {"mcp_test_register_input_handler", (DL_FUNC)&register_input_handler, 2},
    {"mcp_test_read_console_once", (DL_FUNC)&read_console_once, 1},
    {"mcp_test_ignore_sigint", (DL_FUNC)&ignore_sigint, 0},
    {"mcp_test_restore_sigint", (DL_FUNC)&restore_sigint, 0},
    {NULL, NULL, 0},
};

void R_init_mcp_test_input_handler(DllInfo *dll) {
  R_registerRoutines(dll, NULL, call_methods, NULL, NULL);
  R_useDynamicSymbols(dll, FALSE);
}
