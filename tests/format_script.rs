use std::process::Command;

#[test]
fn format_script_skips_missing_formatters() {
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
}
