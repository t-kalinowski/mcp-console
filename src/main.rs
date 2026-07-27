use std::ffi::OsStr;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut arguments = std::env::args_os().skip(1);

    if arguments.next().as_deref() == Some(OsStr::new("--version")) && arguments.next().is_none() {
        println!("mcp-console {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }

    eprintln!("usage: mcp-console --version");
    ExitCode::from(2)
}
