fn main() {
    println!("cargo:rerun-if-changed=src/r_repl.c");

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        cc::Build::new()
            .file("src/r_repl.c")
            .compile("mcp_console_r_repl");
    }
}
