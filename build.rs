use sha2::{Digest as _, Sha256};
use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-changed=src/r_graphics.c");
    println!("cargo:rerun-if-changed=src/r_repl.c");

    if matches!(
        std::env::var("CARGO_CFG_TARGET_OS").as_deref(),
        Ok("macos" | "linux")
    ) {
        bind_private_runner();
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
    let pin_path = root.join("sandbox-runner.json");
    let build_path = root.join("target/sandbox-runner-build.json");
    for path in [&pin_path, &build_path] {
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
    let target = std::env::var("TARGET").expect("Cargo did not provide its build target");
    assert_eq!(
        build["target"].as_str(),
        Some(target.as_str()),
        "private sandbox runner target does not match Cargo TARGET; run scripts/stage-sandbox-runner --target {target}"
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
    let mut artifacts = vec!["mcp-console-sandbox"];
    if target.contains("linux") {
        artifacts.push("bwrap");
    }
    let mut expected_artifacts = Vec::new();
    for name in artifacts {
        let artifact = root.join("wheel-data/data/libexec").join(name);
        println!("cargo:rerun-if-changed={}", artifact.display());
        let bytes = std::fs::read(&artifact)
            .expect("private sandbox artifact is unavailable; run scripts/stage-sandbox-runner");
        let digest: [u8; 32] = Sha256::digest(&bytes).into();
        let actual_digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(
            build["artifacts"][name].as_str(),
            Some(actual_digest.as_str()),
            "private sandbox runner artifact {name} changed; run scripts/stage-sandbox-runner"
        );
        let staged = private_directory.join(format!(".{name}-{}", std::process::id()));
        // Rebuilding must not overwrite an executable used by an active sandbox.
        // Install the verified bytes, without reopening the source for copying.
        std::fs::write(&staged, bytes).expect("failed to stage private sandbox artifact");
        std::fs::set_permissions(&staged, artifact.metadata().unwrap().permissions())
            .expect("failed to set private sandbox artifact permissions");
        std::fs::rename(staged, private_directory.join(name))
            .expect("failed to install private sandbox artifact beside the Cargo output");
        expected_artifacts.push((name, digest));
    }
    std::fs::write(
        output.join("sandbox_runner_installation.rs"),
        format!(
            "pub(super) const PROTOCOL_VERSION: u32 = {protocol};\n\
             const EXPECTED_ARTIFACTS: &[(&str, [u8; 32])] = &{expected_artifacts:?};\n"
        ),
    )
    .expect("failed to bind private sandbox runner installation");
}
