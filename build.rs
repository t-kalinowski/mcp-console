use sha2::{Digest as _, Sha256};
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=src/r_graphics.c");
    println!("cargo:rerun-if-changed=src/r_repl.c");

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        bind_private_runner();
    } else {
        let data =
            PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("wheel-data/data");
        // A macOS build can repopulate these files after Cargo caches this build.
        println!("cargo:rerun-if-changed={}", data.display());
        for directory in ["libexec", "share"] {
            match std::fs::remove_dir_all(data.join(directory)) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => panic!("failed to remove stale {directory} wheel data: {error}"),
            }
        }
    }
    if std::env::var_os("CARGO_CFG_UNIX").is_some() {
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
    let prefix = output.ancestors().nth(4).unwrap();
    let cache = prefix.join("sandbox-runner-cache");
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
    let mut artifacts = String::new();
    for (name, relative) in [
        ("mcp-console-sandbox", "libexec/mcp-console-sandbox"),
        ("LICENSE", "share/licenses/mcp-console/LICENSE"),
        ("NOTICE", "share/licenses/mcp-console/NOTICE"),
    ] {
        let source = stage.join(name);
        let bytes = std::fs::read(&source).unwrap();
        let digest = Sha256::digest(&bytes);
        let digest_hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(build["sha256"][name].as_str(), Some(digest_hex.as_str()));
        artifacts.push_str(&format!("({relative:?}, {:?}),\n", digest.as_slice()));
        // Cargo's native layout and Maturin's wheel data use the same bundle.
        // Wheel packaging requires exclusive use of this source checkout until
        // Maturin finishes writing the archive; see RELEASE.md.
        for destination in [
            prefix.join(relative),
            root.join("wheel-data/data").join(relative),
        ] {
            std::fs::create_dir_all(destination.parent().unwrap()).unwrap();
            std::fs::copy(&source, &destination).unwrap();
            // Restore removed data even when Cargo can reuse the compiled binary.
            println!("cargo:rerun-if-changed={}", destination.display());
        }
    }
    let protocol = pin["protocol_version"].as_u64().unwrap();
    std::fs::write(
        output.join("sandbox_runner_installation.rs"),
        format!(
            "pub(super) const PROTOCOL_VERSION: u32 = {protocol};\n\
             const ARTIFACTS: &[(&str, [u8; 32])] = &[{artifacts}];\n",
        ),
    )
    .expect("failed to bind private sandbox runner installation");
}
