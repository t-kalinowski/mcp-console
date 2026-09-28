.onLoad <- function(libname, pkgname) {
  python <- Sys.getenv("MCP_CONSOLE_TEST_EARLY_PYTHON", unset = "")
  if (nzchar(python)) {
    Sys.setenv(RETICULATE_PYTHON = python)
  }
  reticulate::py_run_string(paste(
    "import json, os, subprocess, sys",
    "early_object = object()",
    "early_identity = id(early_object)",
    "early_executable = sys.executable",
    "early_prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)",
    "early_path = os.environ['PATH']",
    "early_child_program = \"import json, os, sys; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix, os.environ['PATH']]))\"",
    "early_child = json.loads(subprocess.check_output([sys.executable, '-c', early_child_program], text=True))",
    sep = "\n"
  ))
}
