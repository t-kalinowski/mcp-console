use std::ffi::OsStr;
use std::process::ExitCode;

mod sandbox;
mod server;

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();

    match arguments.as_slice() {
        arguments
            if arguments.is_empty()
                || matches!(arguments, [subcommand] if subcommand == OsStr::new("serve")) =>
        {
            match server::run().await {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => {
                    eprintln!("{error}");
                    ExitCode::FAILURE
                }
            }
        }
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
                "usage: mcp-console [serve]\n       mcp-console --version\n       mcp-console sandbox [--] COMMAND [ARG]..."
            );
            ExitCode::from(2)
        }
    }
}
