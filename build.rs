fn main() {
    println!("cargo:rerun-if-changed=src/r_graphics.c");
    println!("cargo:rerun-if-changed=src/r_repl.c");

    cc::Build::new()
        .file("src/r_graphics.c")
        .file("src/r_repl.c")
        .compile("mcp_console_r_repl");
}
