//! Keep embedded resolver source alive through Rscript's complete invocation.
use std::process::Command;

pub(super) struct RProgram {
    #[cfg(windows)]
    _file: tempfile::NamedTempFile,
}

impl RProgram {
    pub(super) fn append(command: &mut Command, source: &str) -> Result<Self, String> {
        #[cfg(unix)]
        {
            command.args(["-e", source]);
            Ok(Self {})
        }
        #[cfg(windows)]
        {
            use std::io::Write;
            // Windows Rscript reparses -e arguments and truncates multiline
            // expressions. Materialize the compile-time source as a script;
            // neither the checkout nor an installation layout is consulted.
            let mut file = tempfile::Builder::new()
                .prefix("mcp-console-resolver-")
                .suffix(".R")
                .tempfile()
                .map_err(|error| error.to_string())?;
            file.write_all(source.as_bytes())
                .map_err(|error| error.to_string())?;
            command.arg(file.path());
            Ok(Self { _file: file })
        }
    }
}
