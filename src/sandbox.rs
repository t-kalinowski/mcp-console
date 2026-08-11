use std::ffi::OsString;
use std::process::ExitCode;

#[cfg(target_os = "macos")]
use std::collections::{BTreeMap, hash_map::DefaultHasher};
#[cfg(target_os = "macos")]
use std::ffi::OsStr;
#[cfg(target_os = "macos")]
use std::fs::{self, DirBuilder, OpenOptions};
#[cfg(target_os = "macos")]
use std::hash::{Hash as _, Hasher as _};
#[cfg(target_os = "macos")]
use std::io::{Read as _, Write as _};
#[cfg(target_os = "macos")]
use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _};
#[cfg(target_os = "macos")]
use std::os::unix::process::CommandExt as _;
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};
#[cfg(target_os = "macos")]
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
#[cfg(target_os = "macos")]
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "macos")]
use wait_timeout::ChildExt as _;

#[cfg(target_os = "macos")]
const MAX_MATPLOTLIB_FONT_CACHE_SIZE: u64 = 16 * 1024 * 1024;
#[cfg(target_os = "macos")]
const MAX_MATPLOTLIB_FONT_CACHE_TOTAL: u64 = 64 * 1024 * 1024;
#[cfg(target_os = "macos")]
const MAX_MATPLOTLIB_FONT_CACHE_FILES: usize = 64;
#[cfg(target_os = "macos")]
const MAX_MATPLOTLIB_CACHE_DIRECTORY_ENTRIES: usize = 128;

#[cfg(target_os = "macos")]
#[path = "sandbox/macos.rs"]
mod platform;

#[cfg(not(target_os = "macos"))]
#[path = "sandbox/unsupported.rs"]
mod platform;

#[cfg(target_os = "macos")]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let mut sandboxed = SandboxedCommand::new(program)?;
    sandboxed
        .args(arguments)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    sandboxed.status()
}

#[cfg(not(target_os = "macos"))]
pub fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    platform::run(command_line)
}

#[cfg(target_os = "macos")]
/// A command configured to run under the macOS sandbox.
///
/// The public sandbox transcript exercises this interaction. This example is
/// ignored as a doctest because the type is crate-private in a binary target.
///
/// # Example
///
/// ```ignore
/// use crate::sandbox::SandboxedCommand;
/// use std::ffi::OsStr;
/// use std::io::{Read, Write};
/// use std::process::Stdio;
///
/// fn read_echo(mut stream: impl Read) -> [u8; 6] {
///     let mut output = [0; 6];
///     stream
///         .read_exact(&mut output)
///         .expect("output should be readable");
///     output
/// }
///
/// let script = r#"
/// import sys
///
/// for line in sys.stdin:
///     if line == "EXIT\n":
///         break
///
///     sys.stdout.write(line)
///     sys.stdout.flush()
///     sys.stderr.write(line)
///     sys.stderr.flush()
/// "#;
///
/// let mut command =
///     SandboxedCommand::new(OsStr::new("python")).expect("sandbox should be configured");
/// command
///     .args(["-c", script])
///     .stdin(Stdio::piped())
///     .stdout(Stdio::piped())
///     .stderr(Stdio::piped());
///
/// let mut child = command.spawn().expect("sandboxed Python should spawn");
/// let mut stdin = child.take_stdin().expect("stdin should be piped");
/// let stdout = child.take_stdout().expect("stdout should be piped");
/// let stderr = child.take_stderr().expect("stderr should be piped");
/// let stdout = std::thread::spawn(move || read_echo(stdout));
/// let stderr = std::thread::spawn(move || read_echo(stderr));
///
/// stdin
///     .write_all(b"hello\n")
///     .expect("input should be written");
/// assert_eq!(stdout.join().expect("stdout reader should finish"), *b"hello\n");
/// assert_eq!(stderr.join().expect("stderr reader should finish"), *b"hello\n");
///
/// stdin
///     .write_all(b"EXIT\n")
///     .expect("EXIT should be written");
/// assert!(child.wait().expect("child should exit").success());
/// ```
pub(crate) struct SandboxedCommand {
    command: Command,
    temporary_directory: platform::TemporaryDirectory,
    matplotlib_cache: Option<MatplotlibCacheGeneration>,
}

#[cfg(target_os = "macos")]
/// A direct sandboxed child that retains its private temporary directory.
///
/// Retain this owner until the child exits, then call `wait`. Dropping it does
/// not terminate the child and removes the private directory. Background
/// descendants are unsupported and may outlive this owner. Piped streams can
/// be taken and moved to independent I/O tasks before waiting.
#[must_use = "retain the sandboxed child until it is explicitly waited"]
pub(crate) struct SandboxedChild {
    child: Child,
    _temporary_directory: platform::TemporaryDirectory,
    matplotlib_cache: Option<MatplotlibCacheGeneration>,
}

#[cfg(target_os = "macos")]
pub(crate) struct MatplotlibCache(PathBuf);

#[cfg(not(target_os = "macos"))]
pub(crate) struct MatplotlibCache;

#[cfg(target_os = "macos")]
struct MatplotlibCacheGeneration {
    persistent_directory: PathBuf,
    private_directory: PathBuf,
    seeded: BTreeMap<OsString, u64>,
}

#[cfg(target_os = "macos")]
impl MatplotlibCache {
    pub(crate) fn from_environment() -> Option<Self> {
        let root = std::env::var_os("XDG_CACHE_HOME")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .or_else(|| {
                std::env::var_os("HOME")
                    .filter(|path| !path.is_empty())
                    .map(PathBuf::from)
                    .filter(|path| path.is_absolute())
                    .map(|path| path.join("Library/Caches"))
            })?;
        Some(Self(root.join("mcp-console/matplotlib")))
    }

    fn seed(&self, temporary_directory: &Path) -> MatplotlibCacheGeneration {
        let private_directory = temporary_directory.join("matplotlib");
        let _ = create_private_directory(&private_directory, false);
        let mut seeded = BTreeMap::new();
        for (name, bytes) in read_font_caches(&self.0) {
            let path = private_directory.join(&name);
            if write_private_file(&path, &bytes).is_ok() {
                seeded.insert(name, cache_fingerprint(&bytes));
            }
        }
        MatplotlibCacheGeneration {
            persistent_directory: self.0.clone(),
            private_directory,
            seeded,
        }
    }
}

#[cfg(not(target_os = "macos"))]
impl MatplotlibCache {
    pub(crate) fn from_environment() -> Option<Self> {
        None
    }
}

#[cfg(target_os = "macos")]
impl MatplotlibCacheGeneration {
    fn promote(mut self) {
        for (name, bytes) in read_font_caches(&self.private_directory) {
            let fingerprint = cache_fingerprint(&bytes);
            if self.seeded.get(&name) == Some(&fingerprint) {
                continue;
            }
            if !self.seeded.contains_key(&name)
                && self.seeded.len() == MAX_MATPLOTLIB_FONT_CACHE_FILES
            {
                continue;
            }
            if publish_font_cache(&self.persistent_directory, &name, &bytes).is_ok() {
                self.seeded.insert(name, fingerprint);
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn read_font_caches(directory: &Path) -> Vec<(OsString, Vec<u8>)> {
    let Ok(metadata) = fs::symlink_metadata(directory) else {
        return Vec::new();
    };
    if !metadata.file_type().is_dir() {
        return Vec::new();
    }
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut paths = Vec::new();
    for (index, entry) in entries
        .take(MAX_MATPLOTLIB_CACHE_DIRECTORY_ENTRIES + 1)
        .enumerate()
    {
        if index == MAX_MATPLOTLIB_CACHE_DIRECTORY_ENTRIES {
            return Vec::new();
        }
        let Ok(entry) = entry else {
            return Vec::new();
        };
        let name = entry.file_name();
        if font_cache_version(&name).is_some() {
            paths.push((name, entry.path()));
        }
    }
    paths.sort_by(|left, right| left.0.cmp(&right.0));

    let mut caches = Vec::new();
    let mut total = 0;
    for (name, path) in paths {
        let (inspected, cache) =
            read_font_cache(&path, &name, MAX_MATPLOTLIB_FONT_CACHE_TOTAL - total);
        total += inspected;
        if total > MAX_MATPLOTLIB_FONT_CACHE_TOTAL {
            break;
        }
        let Some(bytes) = cache else {
            continue;
        };
        caches.push((name, bytes));
        if caches.len() == MAX_MATPLOTLIB_FONT_CACHE_FILES {
            break;
        }
    }
    caches
}

#[cfg(target_os = "macos")]
fn font_cache_version(name: &OsStr) -> Option<&str> {
    let name = name.to_str()?;
    let version = name.strip_prefix("fontlist-v")?.strip_suffix(".json")?;
    (!version.is_empty()
        && version.split('.').all(|component| {
            !component.is_empty() && component.bytes().all(|byte| byte.is_ascii_digit())
        }))
    .then_some(version)
}

#[cfg(target_os = "macos")]
fn cache_fingerprint(bytes: &[u8]) -> u64 {
    let mut hasher = DefaultHasher::new();
    bytes.hash(&mut hasher);
    hasher.finish()
}

#[cfg(target_os = "macos")]
fn read_font_cache(path: &Path, name: &OsStr, remaining: u64) -> (u64, Option<Vec<u8>>) {
    let Some(version) = font_cache_version(name) else {
        return (0, None);
    };
    let Ok(file) = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
    else {
        return (0, None);
    };
    let Ok(metadata) = file.metadata() else {
        return (0, None);
    };
    if !metadata.is_file() {
        return (0, None);
    }
    let limit = remaining.min(MAX_MATPLOTLIB_FONT_CACHE_SIZE);
    if metadata.len() > limit {
        return (limit + 1, None);
    }
    let mut bytes = Vec::new();
    if file.take(limit + 1).read_to_end(&mut bytes).is_err() {
        return (bytes.len() as u64, None);
    }
    let inspected = bytes.len() as u64;
    if inspected > limit {
        return (inspected, None);
    }
    let valid = serde_json::from_slice::<serde_json::Value>(&bytes)
        .ok()
        .is_some_and(|value| valid_font_manager(&value, version));
    (inspected, valid.then_some(bytes))
}

#[cfg(target_os = "macos")]
fn valid_font_manager(value: &serde_json::Value, version: &str) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    if object.get("__class__").and_then(|value| value.as_str()) != Some("FontManager") {
        return false;
    }
    let Some(cached_version) = object.get("_version") else {
        return false;
    };
    let matches = cached_version.as_str() == Some(version)
        || cached_version
            .as_u64()
            .is_some_and(|cached| cached.to_string() == version);
    let default_weight = object.get("_FontManager__default_weight");
    let default_size = object.get("default_size");
    let default_family = object
        .get("defaultFamily")
        .and_then(|value| value.as_object());
    let afm = object.get("afmlist").and_then(|value| value.as_array());
    let ttf = object.get("ttflist").and_then(|value| value.as_array());
    matches
        && default_weight.is_some_and(|value| value.is_string() || value.is_number())
        && default_size.is_some_and(|value| value.is_null() || value.is_number())
        && default_family.is_some_and(|family| {
            ["ttf", "afm"]
                .iter()
                .all(|name| family.get(*name).is_some_and(serde_json::Value::is_string))
        })
        && afm.is_some_and(|entries| entries.iter().all(valid_font_entry))
        && ttf.is_some_and(|entries| !entries.is_empty() && entries.iter().all(valid_font_entry))
}

#[cfg(target_os = "macos")]
fn valid_font_entry(value: &serde_json::Value) -> bool {
    let Some(entry) = value.as_object() else {
        return false;
    };
    entry.get("__class__").and_then(|value| value.as_str()) == Some("FontEntry")
        && ["fname", "name", "style", "variant", "stretch", "size"]
            .iter()
            .all(|name| entry.get(*name).is_some_and(serde_json::Value::is_string))
        && entry.get("index").is_none_or(serde_json::Value::is_number)
        && entry
            .get("weight")
            .is_some_and(|value| value.is_string() || value.is_number())
}

#[cfg(target_os = "macos")]
fn publish_font_cache(directory: &Path, name: &OsStr, bytes: &[u8]) -> std::io::Result<()> {
    create_private_directory(directory, true)?;
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let mut staging_name = OsString::from(".mcp-console-font-cache-");
    staging_name.push(std::process::id().to_string());
    staging_name.push("-");
    staging_name.push(unique.to_string());
    let staging = directory.join(staging_name);
    if let Err(error) = write_private_file(&staging, bytes) {
        let _ = fs::remove_file(staging);
        return Err(error);
    }
    let destination = directory.join(name);
    if let Err(error) = fs::rename(&staging, destination) {
        let _ = fs::remove_file(staging);
        return Err(error);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn create_private_directory(path: &Path, recursive: bool) -> std::io::Result<()> {
    let mut builder = DirBuilder::new();
    builder.recursive(recursive).mode(0o700).create(path)
}

#[cfg(target_os = "macos")]
fn write_private_file(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    file.flush()
}

#[cfg(target_os = "macos")]
impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (command, temporary_directory) = platform::sandboxed_command()?;
        let temporary_directory_path = temporary_directory.path().as_os_str().to_os_string();
        let mut sandboxed = Self {
            command,
            temporary_directory,
            matplotlib_cache: None,
        };
        sandboxed
            .env("TMPDIR", temporary_directory_path)
            .arg(program);
        Ok(sandboxed)
    }

    pub(crate) fn arg(&mut self, argument: impl AsRef<OsStr>) -> &mut Self {
        self.command.arg(argument);
        self
    }

    pub(crate) fn args<I, S>(&mut self, arguments: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        for argument in arguments {
            self.arg(argument);
        }
        self
    }

    /// Adds an environment variable inherited by the sandboxed program.
    ///
    /// macOS filters `DYLD_*` variables when launching `sandbox-exec`; this
    /// wrapper intentionally does not restore them inside the sandbox.
    /// `TMPDIR` is reserved and reset to the private directory when spawning.
    pub(crate) fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        self.command.env(key, value);
        self
    }

    pub(crate) fn stdin(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdin(configuration);
        self
    }

    pub(crate) fn stdout(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdout(configuration);
        self
    }

    pub(crate) fn stderr(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stderr(configuration);
        self
    }

    /// Isolates a background sandbox command for bounded forced termination.
    pub(crate) fn new_process_group(&mut self) -> &mut Self {
        self.command.process_group(0);
        self
    }

    pub(crate) fn seed_matplotlib_cache(&mut self, cache: &MatplotlibCache) -> &mut Self {
        self.matplotlib_cache = Some(cache.seed(self.temporary_directory.path()));
        self
    }

    /// Spawns the sandboxed program and transfers the temporary-directory
    /// guard to the returned child.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        Ok(SandboxedChild {
            child,
            _temporary_directory: self.temporary_directory,
            matplotlib_cache: self.matplotlib_cache,
        })
    }

    pub(crate) fn status(self) -> Result<ExitCode, String> {
        let status = self.spawn()?.wait()?;
        Ok(platform::exit_code(status))
    }
}

#[cfg(target_os = "macos")]
impl SandboxedChild {
    #[allow(dead_code, reason = "used by spawned callers with piped stdin")]
    pub(crate) fn take_stdin(&mut self) -> Option<ChildStdin> {
        self.child.stdin.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stdout")]
    pub(crate) fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    #[allow(dead_code, reason = "used by spawned callers with piped stderr")]
    pub(crate) fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }

    pub(crate) fn wait(mut self) -> Result<ExitStatus, String> {
        let status = self
            .child
            .wait()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        self.promote_matplotlib_cache();
        Ok(status)
    }

    /// Waits at most `timeout` for the direct sandbox process to exit.
    pub(crate) fn wait_timeout(&mut self, timeout: Duration) -> Result<Option<ExitStatus>, String> {
        let status = self.child.wait_timeout(timeout).map_err(|error| {
            format!(
                "failed to wait for `{}` to exit: {error}",
                platform::SANDBOX_EXEC
            )
        })?;
        if status.is_some() {
            self.promote_matplotlib_cache();
        }
        Ok(status)
    }

    /// Kills the live sandbox process group and reaps its direct process.
    ///
    /// Full descendant supervision, including a group whose leader has already
    /// exited, belongs to the sandbox lifetime supervisor.
    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        match self.child.try_wait() {
            Ok(Some(_)) => {
                self.promote_matplotlib_cache();
                return Ok(());
            }
            Ok(None) => {}
            Err(error) => {
                return Err(format!(
                    "failed to read `{}` status before stopping it: {error}",
                    platform::SANDBOX_EXEC
                ));
            }
        }

        // SAFETY: `new_process_group` made the child's PID its process-group ID.
        let result = unsafe { libc::killpg(self.child.id() as libc::pid_t, libc::SIGKILL) };
        if result < 0 {
            let kill_error = std::io::Error::last_os_error();
            return match self.child.try_wait() {
                Ok(Some(_)) => {
                    self.promote_matplotlib_cache();
                    Ok(())
                }
                Ok(None) => Err(format!(
                    "failed to stop `{}`: {kill_error}",
                    platform::SANDBOX_EXEC
                )),
                Err(wait_error) => Err(format!(
                    "failed to stop `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    platform::SANDBOX_EXEC
                )),
            };
        }

        self.child.wait().map_err(|error| {
            format!(
                "failed to reap stopped `{}`: {error}",
                platform::SANDBOX_EXEC
            )
        })?;
        self.promote_matplotlib_cache();
        Ok(())
    }

    fn promote_matplotlib_cache(&mut self) {
        if let Some(cache) = self.matplotlib_cache.take() {
            cache.promote();
        }
    }
}
