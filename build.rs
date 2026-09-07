use sha2::{Digest as _, Sha256};
use std::path::PathBuf;

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
    let pin_path = root.join("sandbox-runner.json");
    let build_path = root.join("target/sandbox-runner-build.json");
    let runner_path = root.join("target/private-wheel-data/data/libexec/mcp-console-sandbox");
    for path in [&pin_path, &build_path, &runner_path] {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let pin: serde_json::Value = serde_json::from_slice(
        &std::fs::read(pin_path).expect("failed to read sandbox-runner.json"),
    )
    .expect("invalid sandbox-runner.json");
    let build: serde_json::Value = serde_json::from_slice(
        &std::fs::read(build_path)
            .expect("private sandbox runner is not staged; run scripts/stage-sandbox-runner"),
    )
    .expect("invalid private sandbox runner build manifest");
    let revision = pin["commit"]
        .as_str()
        .expect("sandbox-runner.json must contain a source commit");
    assert_eq!(
        build["source_revision"].as_str(),
        Some(revision),
        "private sandbox runner source pin changed; run scripts/stage-sandbox-runner"
    );
    let bytes = std::fs::read(&runner_path)
        .expect("private sandbox runner is unavailable; run scripts/stage-sandbox-runner");
    let digest = Sha256::digest(bytes);
    let actual_digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    assert_eq!(
        build["sha256"].as_str(),
        Some(actual_digest.as_str()),
        "private sandbox runner artifact changed; run scripts/stage-sandbox-runner"
    );
    let protocol = u32::try_from(
        pin["protocol_version"]
            .as_u64()
            .expect("sandbox-runner.json must contain a protocol version"),
    )
    .expect("sandbox runner protocol version exceeds u32");
    let output = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    // Cargo places OUT_DIR under <prefix>/<profile>/build/<package>/out.
    // Match the installed bin/../libexec layout for every Cargo profile.
    let prefix = output
        .ancestors()
        .nth(4)
        .expect("Cargo OUT_DIR is missing its target prefix");
    let private_directory = prefix.join("libexec");
    std::fs::create_dir_all(&private_directory)
        .expect("failed to create the private sandbox runner directory");
    std::fs::copy(&runner_path, private_directory.join("mcp-console-sandbox"))
        .expect("failed to install the private sandbox runner beside the Cargo output");
    std::fs::write(
        output.join("sandbox_runner_installation.rs"),
        format!(
            "pub(super) const PROTOCOL_VERSION: u32 = {protocol};\n\
             const EXPECTED_RUNNER_SHA256: [u8; 32] = {:?};\n",
            digest.as_slice()
        ),
    )
    .expect("failed to bind private sandbox runner installation");
}
