#include <R.h>
#include <R_ext/Rdynload.h>
#include <R_ext/eventloop.h>
#include <Rinternals.h>

#include <fcntl.h>
#include <unistd.h>

static InputHandler *registered_handler = NULL;
static int registered_fd = -1;

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
  Rprintf("cell start callback\n");
}

static SEXP register_input_handler(SEXP path) {
  if (registered_handler != NULL) {
    Rf_error("test input handler is already registered");
  }

  int fd = open(CHAR(STRING_ELT(path, 0)), O_RDONLY | O_NONBLOCK);
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
  return R_NilValue;
}

static const R_CallMethodDef call_methods[] = {
    {"mcp_test_register_input_handler", (DL_FUNC)&register_input_handler, 1},
    {NULL, NULL, 0},
};

void R_init_mcp_test_input_handler(DllInfo *dll) {
  R_registerRoutines(dll, NULL, call_methods, NULL, NULL);
  R_useDynamicSymbols(dll, FALSE);
}
