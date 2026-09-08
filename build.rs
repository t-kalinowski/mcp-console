use sha2::{Digest as _, Sha256};
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=src/r_graphics.c");
    println!("cargo:rerun-if-changed=src/r_repl.c");

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        bind_private_runner();
        cc::Build::new()
            .file("src/r_graphics.c")
            .file("src/r_repl.c")
            .compile("mcp_console_r_repl");
    }
}

fn bind_private_runner() {
    let root = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let output = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let stage = output.join("sandbox-runner");
    // OUT_DIR is <target prefix>/<profile>/build/<package>/out.
    let cache = output
        .ancestors()
        .nth(4)
        .unwrap()
        .join("sandbox-runner-cache");
    println!("cargo:rerun-if-changed=sandbox-runner.json");
    println!("cargo:rerun-if-changed=scripts/stage-sandbox-runner");
    println!("cargo:rerun-if-env-changed=MCP_CONSOLE_SANDBOX_SOURCE");
    let target = std::env::var("TARGET").expect("Cargo did not provide its build target");
    let status = Command::new("python3")
        .arg(root.join("scripts/stage-sandbox-runner"))
        .arg("--target")
        .arg(&target)
        .arg("--cache-dir")
        .arg(cache)
        .arg("--output-dir")
        .arg(&stage)
        .status()
        .expect("building the private sandbox runner requires Python 3, Git, and rustup");
    assert!(status.success(), "private sandbox runner build failed");

    let pin: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.join("sandbox-runner.json")).unwrap()).unwrap();
    let build: serde_json::Value =
        serde_json::from_slice(&std::fs::read(stage.join("build.json")).unwrap()).unwrap();
    assert_eq!(build["source_revision"], pin["commit"]);
    assert_eq!(build["target"].as_str(), Some(target.as_str()));
    let digest = Sha256::digest(std::fs::read(stage.join("mcp-console-sandbox")).unwrap());
    let digest_hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    assert_eq!(build["sha256"].as_str(), Some(digest_hex.as_str()));
    let mut bundle = Sha256::new();
    for name in ["mcp-console-sandbox", "LICENSE", "NOTICE"] {
        let bytes = std::fs::read(stage.join(name)).unwrap();
        bundle.update((bytes.len() as u64).to_be_bytes());
        bundle.update(bytes);
    }
    let bundle_hex: String = bundle
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let protocol = pin["protocol_version"].as_u64().unwrap();
    std::fs::write(
        output.join("sandbox_runner_installation.rs"),
        format!(
            "pub(super) const PROTOCOL_VERSION: u32 = {protocol};\n\
             const BUNDLE_SHA256: &str = {bundle_hex:?};\n",
        ),
    )
    .expect("failed to bind private sandbox runner installation");
}
