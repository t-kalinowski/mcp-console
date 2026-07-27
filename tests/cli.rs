use std::process::Command;

#[test]
fn version_reports_the_binary_name_and_package_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("version")
        .output()
        .expect("mcp-console should run");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be UTF-8"),
        format!("mcp-console {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert!(output.stderr.is_empty());
}
