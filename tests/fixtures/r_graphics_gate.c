#include <R.h>
#include <R_ext/Rdynload.h>
#include <Rinternals.h>

#include <errno.h>
#include <fcntl.h>
#include <unistd.h>

/* Model native work without an R interrupt checkpoint. No signal handler or
 * interpreter call runs inside the wait; EINTR alone does not release it. */
static SEXP wait_graphics_gate(SEXP started, SEXP release) {
  int ready = open(CHAR(STRING_ELT(started, 0)), O_WRONLY);
  if (ready < 0) Rf_error("failed to open graphics checkpoint");
  ssize_t sent = write(ready, "1", 1);
  close(ready);
  if (sent != 1) Rf_error("failed to write graphics checkpoint");

  int gate = open(CHAR(STRING_ELT(release, 0)), O_RDONLY);
  if (gate < 0) Rf_error("failed to open graphics release gate");
  char byte;
  ssize_t count;
  do {
    count = read(gate, &byte, 1);
  } while (count < 0 && errno == EINTR);
  close(gate);
  if (count != 1 || byte != '1') Rf_error("failed to read graphics release gate");
  return R_NilValue;
}

static const R_CallMethodDef call_methods[] = {
    {"mcp_test_wait_graphics_gate", (DL_FUNC)&wait_graphics_gate, 2},
    {NULL, NULL, 0},
};

void R_init_mcp_test_graphics_gate(DllInfo *dll) {
  R_registerRoutines(dll, NULL, call_methods, NULL, NULL);
  R_useDynamicSymbols(dll, FALSE);
}
