use std::ffi::OsStr;
use std::process::ExitCode;

mod sandbox;

fn main() -> ExitCode {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();

    match arguments.as_slice() {
        [argument] if argument == OsStr::new("--version") => {
            println!("mcp-console {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        [subcommand, command @ ..] if subcommand == OsStr::new("sandbox") => {
            if command.is_empty() || matches!(command, [separator] if separator == OsStr::new("--"))
            {
                eprintln!("usage: mcp-console sandbox [--] COMMAND [ARG]...");
                ExitCode::from(2)
            } else {
                match sandbox::run(command) {
                    Ok(exit_code) => exit_code,
                    Err(error) => {
                        eprintln!("{error}");
                        ExitCode::FAILURE
                    }
                }
            }
        }
        _ => {
            eprintln!(
                "usage: mcp-console --version\n       mcp-console sandbox [--] COMMAND [ARG]..."
            );
            ExitCode::from(2)
        }
    }
}
