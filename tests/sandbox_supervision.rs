#![cfg(target_os = "macos")]

use std::io::{BufRead, BufReader, Read};
use std::process::{Command, Stdio};

#[test]
fn sandbox_reports_when_root_termination_cannot_be_confirmed() {
    // Permit the launcher's descriptor scan, but deny child inspection and
    // signaling so both public error paths are deterministic.
    let outer_policy = r#"
(version 1)
(allow default)
(deny signal)
(deny process-info*)
(allow process-info* (target self))
"#;
    let output = Command::new("/usr/bin/sandbox-exec")
        .args(["-p", outer_policy])
        .arg(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "/bin/sleep", "1"])
        .output()
        .expect("outer sandbox should run");

    let stderr = String::from_utf8(output.stderr).expect("sandbox error should be UTF-8");
    assert_eq!(output.status.code(), Some(1));
    assert!(stderr.contains("failed to inspect sandbox process"));
    assert!(
        stderr.contains(
            "additionally, failed to terminate the `/usr/bin/sandbox-exec` process group"
        ),
        "sandbox did not report its termination failure: {stderr}"
    );
}

#[test]
fn sandbox_terminates_processx_descendants_before_returning() {
    let script = r#"
child_script <- '
p <- processx::process$new("/bin/sleep", "60", cleanup = FALSE)
writeLines(as.character(p$get_pid()))
flush.console()
Sys.sleep(60)
'
child <- processx::process$new(
    "Rscript",
    c("-e", child_script),
    stdout = "|",
    cleanup = FALSE
)
stopifnot(child$poll_io(5000)[["output"]] == "ready")
grandchild_pid <- child$read_output_lines()
stopifnot(length(grandchild_pid) == 1)
writeLines(c(as.character(child$get_pid()), grandchild_pid))
quit(save = "no", status = 23, runLast = FALSE)
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    let stdout = String::from_utf8(output.stdout);
    let reported_pids: Vec<libc::pid_t> = stdout
        .as_deref()
        .unwrap_or_default()
        .lines()
        .filter_map(|pid| pid.parse().ok())
        .collect();
    let survivors: Vec<_> = reported_pids
        .iter()
        .copied()
        .filter(|pid| unsafe { libc::kill(*pid, 0) } == 0)
        .collect();
    for pid in &survivors {
        let _ = unsafe { libc::kill(*pid, libc::SIGKILL) };
    }

    assert_eq!(
        output.status.code(),
        Some(23),
        "sandboxed R failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let pids: Vec<libc::pid_t> = stdout
        .expect("descendant PIDs should be UTF-8")
        .lines()
        .map(|pid| {
            pid.parse()
                .expect("sandbox should report numeric descendant PIDs")
        })
        .collect();
    assert_eq!(pids.len(), 2, "sandbox should report two descendant PIDs");
    assert!(
        survivors.is_empty(),
        "sandbox descendants {survivors:?} survived the launcher"
    );
}

#[test]
fn sandbox_terminates_processx_descendants_when_interrupted() {
    let script = r#"
p <- processx::process$new("/bin/sleep", "60", cleanup = FALSE)
writeLines(as.character(p$get_pid()))
flush.console()
Sys.sleep(60)
"#;
    let mut launcher = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("mcp-console sandbox should start");

    let mut stdout = BufReader::new(
        launcher
            .stdout
            .take()
            .expect("sandbox stdout should be piped"),
    );
    let mut pid = String::new();
    stdout
        .read_line(&mut pid)
        .expect("sandbox should report its descendant PID");
    let pid = pid
        .trim()
        .parse::<libc::pid_t>()
        .expect("sandbox should report a descendant PID");

    let signal_result = unsafe { libc::kill(launcher.id() as libc::pid_t, libc::SIGINT) };
    assert_eq!(signal_result, 0, "sandbox launcher should receive SIGINT");

    let status = launcher.wait().expect("mcp-console sandbox should exit");
    let mut stderr = String::new();
    launcher
        .stderr
        .take()
        .expect("sandbox stderr should be piped")
        .read_to_string(&mut stderr)
        .expect("sandbox stderr should be readable");
    let descendant_is_alive = unsafe { libc::kill(pid, 0) } == 0;
    if descendant_is_alive {
        let _ = unsafe { libc::kill(pid, libc::SIGKILL) };
    }

    assert!(
        status.code().is_some(),
        "the launcher should survive SIGINT and finish cleanup"
    );
    assert!(!status.success(), "sandboxed R should be interrupted");
    assert!(
        !descendant_is_alive,
        "sandbox descendant {pid} survived SIGINT; stderr: {stderr}"
    );
}
