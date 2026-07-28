#![cfg(target_os = "macos")]

use std::process::Command;

#[test]
fn sandbox_read_only_policy_allows_python_multiprocessing_semaphores() {
    let script = r#"
import multiprocessing as mp
import operator

context = mp.get_context("spawn")
lock = context.Lock()
lock.acquire()
child = context.Process(target=operator.methodcaller("release"), args=(lock,))
child.start()
child.join()
assert child.exitcode == 0
assert lock.acquire(timeout=1)
print("semaphore shared")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"semaphore shared\n");
    assert!(output.stderr.is_empty());
}

#[test]
fn sandbox_does_not_require_home() {
    let script = r#"
print("ran")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .env_remove("HOME")
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(output.stdout, b"ran\n");
    assert!(output.stderr.is_empty());
}

#[test]
fn sandbox_supports_r_runtime_queries_and_temporary_writes() {
    let script = r#"
stopifnot(parallel::detectCores() >= 1)

output <- file.path(tempdir(), "result.txt")
writeLines("sandboxed R", output)
writeLines(readLines(output))
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed R failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed R\n");
}

#[test]
fn sandbox_read_only_policy_allows_processx_pty_processes() {
    let script = r#"
p <- processx::process$new("/bin/cat", pty = TRUE)
on.exit(if (p$is_alive()) p$kill())
p$write_input("sandboxed pty\n")
stopifnot(p$poll_io(5000)[["output"]] == "ready")
cat(p$read_output())
invisible(p$kill())
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed processx failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed pty\r\n");
}
