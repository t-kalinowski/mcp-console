use std::process::Command;

#[test]
fn format_script_calls_ruff_directly_and_attempts_all_formatters_when_missing() {
    let script = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/format");
    let output = Command::new("/bin/sh")
        .arg(script)
        .env("PATH", "")
        .output()
        .expect("format script should run");

    assert!(
        output.status.success(),
        "format script failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let stderr = String::from_utf8(output.stderr).expect("stderr should be UTF-8");
    for formatter in ["ruff", "yamark", "cargo", "air"] {
        assert!(
            stderr.contains(formatter),
            "format script did not attempt {formatter}: {stderr}"
        );
    }
}
