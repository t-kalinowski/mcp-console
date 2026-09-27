//! Trusted launch settings, independent of submitted requirements and worker grants.

use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Settings {
    /// Native proxy host patterns, in addition to the built-in package sources.
    pub(crate) allowed_hosts: Vec<String>,
    /// Explicit resolver settings; these reach only the enforced workload.
    pub(crate) environment: BTreeMap<String, String>,
    /// Additional trusted runtime or certificate locations, never write grants.
    pub(crate) readable_roots: Vec<PathBuf>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Launch {
    pub(crate) no_sandbox: bool,
    pub(crate) custom_worker: bool,
    pub(crate) workspace: PathBuf,
    pub(crate) cache_home: Option<PathBuf>,
    #[serde(with = "super::data::environment")]
    pub(crate) environment: BTreeMap<OsString, OsString>,
    pub(crate) settings: Settings,
    pub(crate) readable: Vec<PathBuf>,
}

impl Launch {
    pub(crate) fn capture(no_sandbox: bool, settings: Settings) -> Result<Self, String> {
        let workspace = std::env::current_dir().map_err(|e| e.to_string())?;
        // Direct execution preserves the caller's complete native environment.
        // Resolver policy projection and read grants apply only to native mode.
        if no_sandbox {
            let mut environment = std::env::vars_os().collect::<BTreeMap<_, _>>();
            environment.extend(
                settings
                    .environment
                    .iter()
                    .map(|(name, value)| (name.into(), value.into())),
            );
            let search_path = environment
                .get(OsStr::new("PATH"))
                .cloned()
                .unwrap_or_default();
            if let Some(python) = ["python3", "python"].into_iter().find_map(|program| {
                std::env::split_paths(&search_path)
                    .map(|path| workspace.join(path).join(program))
                    .find(|path| std::fs::symlink_metadata(path).is_ok())
            }) {
                environment.insert(
                    "MCP_CONSOLE_RESOLVER_PYTHON".into(),
                    python.into_os_string(),
                );
            }
            return Ok(Self {
                no_sandbox,
                custom_worker: false,
                workspace,
                cache_home: None,
                environment,
                settings,
                readable: Vec::new(),
            });
        }
        let cache_home = std::env::var_os("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")));
        let mut environment = BTreeMap::new();
        for (name, value) in std::env::vars_os() {
            let (Some(name), Some(value)) = (name.to_str(), value.to_str()) else {
                continue;
            };
            if [
                "UV_",
                "IR_",
                "RENV_",
                "PKG_",
                "R_",
                "PYTHON",
                "RETICULATE_",
                "LD_",
                "DYLD_",
            ]
            .iter()
            .any(|prefix| name.starts_with(prefix))
                || matches!(
                    name,
                    "PATH"
                        | "HOME"
                        | "LANG"
                        | "LC_ALL"
                        | "SSL_CERT_FILE"
                        | "SSL_CERT_DIR"
                        | "CURL_CA_BUNDLE"
                        | "CC"
                        | "CXX"
                        | "SDKROOT"
                )
            {
                environment.insert(name.into(), value.into());
            }
        }
        environment.extend(settings.environment.clone());
        let search_path = environment.get("PATH").cloned().unwrap_or_default();
        let find = |program: &str| {
            std::env::split_paths(&search_path)
                .map(|path| workspace.join(path).join(program))
                .find(|path| std::fs::symlink_metadata(path).is_ok())
        };
        // Capture executable choices before any worker can alter PATH entries.
        let mut readable = settings
            .readable_roots
            .iter()
            .map(|path| workspace.join(path))
            .collect::<Vec<_>>();
        for (program, variable) in [
            ("uv", "RETICULATE_UV"),
            ("R", "MCP_CONSOLE_RESOLVER_R"),
            ("ir", "MCP_CONSOLE_RESOLVER_IR"),
        ] {
            let selected = environment
                .get(variable)
                .filter(|s| s.as_str() != "managed")
                .map(PathBuf::from)
                .or_else(|| find(program));
            if let Some(path) = selected {
                let path = std::path::absolute(path).map_err(|e| e.to_string())?;
                let target = path
                    .canonicalize()
                    .map_err(|e| format!("cannot locate resolver {program}: {e}"))?;
                // Keep reticulate's explicit managed selection. The PATH uv is
                // still readable for ir bootstrap and Python-only discovery.
                if environment.get(variable).map(String::as_str) != Some("managed") {
                    environment.insert(variable.into(), path.to_string_lossy().into_owned());
                }
                readable.push(path);
                readable.push(target);
            }
        }
        for variable in ["R_HOME"] {
            if let Some(value) = environment.get(variable) {
                readable
                    .extend(std::env::split_paths(value).filter(|p| p.is_absolute() && p.exists()));
            }
        }
        if !environment.contains_key("MCP_CONSOLE_RESOLVER_R")
            && !environment.contains_key("R_HOME")
            && !environment.contains_key("RETICULATE_UV")
            && !environment
                .get("RETICULATE_PYTHON")
                .is_some_and(|s| !s.is_empty() && s != "managed")
            && let Some(python) = find("python3").or_else(|| find("python"))
        {
            environment.insert(
                "MCP_CONSOLE_RESOLVER_PYTHON".into(),
                workspace.join(python).to_string_lossy().into_owned(),
            );
        }
        if let Some(python) = environment
            .get("RETICULATE_PYTHON")
            .filter(|s| !s.is_empty() && s.as_str() != "managed")
            .or_else(|| environment.get("MCP_CONSOLE_RESOLVER_PYTHON"))
        {
            let python = PathBuf::from(python);
            let selected = if python.components().count() == 1 {
                find(python.to_str().ok_or("Python selection must be UTF-8")?)
            } else {
                Some(workspace.join(python))
            };
            // R sessions retain an opaque Python selection until first use.
            // A missing selection grants no reads; runtime inspection owns its
            // eventual error, so R-only cells remain usable.
            if let Some(python) = selected.filter(|p| p.exists()) {
                let variable = if environment.contains_key("MCP_CONSOLE_RESOLVER_PYTHON") {
                    "MCP_CONSOLE_RESOLVER_PYTHON"
                } else {
                    "RETICULATE_PYTHON"
                };
                environment.insert(variable.into(), python.to_string_lossy().into_owned());
                readable.extend(python_reads(&python)?);
            }
        }
        Ok(Self {
            no_sandbox,
            custom_worker: false,
            workspace,
            cache_home,
            environment: environment
                .into_iter()
                .map(|(name, value)| (name.into(), value.into()))
                .collect(),
            settings,
            readable,
        })
    }

    pub(super) fn native(
        &self,
        storage: &super::storage::Storage,
        executable: &Path,
        version: u32,
    ) -> Result<Value, String> {
        let payload = &storage.payload;
        let mut environment = self
            .environment
            .iter()
            .map(|(name, value)| {
                let utf8 = |value: &OsStr| {
                    value
                        .to_str()
                        .map(str::to_owned)
                        .ok_or("native resolver environment must be UTF-8")
                };
                Ok((utf8(name)?, utf8(value)?))
            })
            .collect::<Result<BTreeMap<String, String>, &str>>()?;
        for (name, child) in [
            ("HOME", "home"),
            ("R_USER", "home"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_DATA_HOME", "data"),
            ("UV_CACHE_DIR", "uv/cache"),
            ("UV_PYTHON_INSTALL_DIR", "uv/python"),
            ("UV_TOOL_DIR", "uv/tools"),
            ("UV_TOOL_BIN_DIR", "uv/bin"),
            ("IR_CACHE_DIR", "ir"),
            ("IR_LIBRARY_ROOT", "ir/libraries"),
            ("R_LIBS", "r/bootstrap"),
            ("R_USER_CACHE_DIR", "r/cache"),
            ("R_USER_CONFIG_DIR", "r/config"),
            ("R_USER_DATA_DIR", "r/data"),
            ("RENV_PATHS_ROOT", "renv"),
            ("RENV_PATHS_CACHE", "renv/cache"),
            ("PKG_CACHE_DIR", "pak"),
            ("R_PKG_CACHE_DIR", "pak"),
            ("MPLCONFIGDIR", "matplotlib"),
            ("MCP_CONSOLE_EXTENSION_DIRECTORY", "extensions"),
        ] {
            environment.insert(
                name.into(),
                payload.join(child).to_string_lossy().into_owned(),
            );
        }
        // These select configuration discovery and retention, not enforcement.
        environment.insert("UV_NO_CONFIG".into(), "1".into());
        environment.insert("UV_NO_ENV_FILE".into(), "1".into());
        environment.insert("UV_NO_CACHE".into(), "0".into());
        environment.insert("R_LIBS_USER".into(), "NULL".into());
        environment.insert("R_LIBS_SITE".into(), "NULL".into());
        environment.insert(
            "MCP_CONSOLE_RESOLVER_PAYLOAD".into(),
            payload.to_string_lossy().into_owned(),
        );
        let mut domains = BTreeMap::new();
        for host in [
            "pypi.org",
            "files.pythonhosted.org",
            "github.com",
            "api.github.com",
            "codeload.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "r-lib.github.io",
            "packagemanager.posit.co",
            "rspm-sync.rstudio.com",
            "bioconductor.posit.co",
            "bioconductor.org",
            "cran.r-project.org",
            "cloud.r-project.org",
            "cran.rstudio.com",
            "extensions.duckdb.org",
        ] {
            domains.insert(host.to_owned(), "allow");
        }
        for host in &self.settings.allowed_hosts {
            domains.insert(host.clone(), "allow");
        }
        let mut entries = Vec::new();
        let system = [
            "/usr/bin",
            "/usr/sbin",
            "/usr/lib",
            "/usr/lib64",
            "/usr/share",
            "/usr/include",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/System",
            "/Library/Frameworks",
            "/Library/Developer/CommandLineTools",
            "/Applications/Xcode.app",
            "/opt/homebrew/bin",
            "/opt/homebrew/opt",
            "/opt/homebrew/lib",
            "/opt/homebrew/Cellar",
            "/opt/R",
            "/etc/ssl/certs",
            "/etc/ssl/cert.pem",
            "/etc/ssl/openssl.cnf",
            "/etc/pki/tls/certs",
            "/etc/pki/tls/cert.pem",
            "/etc/pki/tls/openssl.cnf",
            "/etc/pki/ca-trust/extracted",
            "/etc/ld.so.cache",
            "/etc/ld.so.conf",
            "/etc/ld.so.conf.d",
            "/etc/resolv.conf",
            "/etc/nsswitch.conf",
            "/etc/hosts",
            "/etc/localtime",
            "/dev/null",
            "/dev/zero",
            "/dev/random",
            "/dev/urandom",
        ];
        for path in self
            .readable
            .iter()
            .map(PathBuf::as_path)
            .chain(std::iter::once(executable))
            .chain(system.into_iter().map(Path::new).filter(|p| p.exists()))
        {
            entries.push(json!({"path":{"type":"path","path":path},"access":"read"}));
        }
        entries.push(json!({"path":{"type":"path","path":payload},"access":"write"}));
        let policy = json!({
            "version": version,
            "filesystem": {"kind":"restricted","entries":entries},
            "network":"restricted",
            // renv uses ephemeral loopback sockets for subprocess coordination.
            // macOS shares host loopback; Linux uses a private network namespace.
            "proxy": {"enabled":true,"enableSocks5":false,"enableSocks5Udp":false,"allowUpstreamProxy":false,"dangerouslyAllowAllUnixSockets":false,"mode":"full","domains":domains,"allowLocalBinding":true},
            "inherit_environment":false,"environment":environment,
            "lifecycle":{"parent_pid":std::process::id(),"sigterm":"retire","private_tmp":{"parent":payload,"environment":["TMPDIR","TMP","TEMP"]}}
        });
        #[cfg(target_os = "macos")]
        let policy = {
            let mut policy = policy;
            policy["macos_seatbelt_profile_extension"] = include_str!("platform.sbpl").into();
            policy
        };
        Ok(policy)
    }
}

fn python_reads(python: &Path) -> Result<Vec<PathBuf>, String> {
    // An executable alias does not grant its parent directory. Recognize only
    // standard bin/lib layouts, without following library-directory symlinks
    // or interpreting an untrusted pyvenv.cfg as additional host permissions.
    let mut paths = Vec::new();
    for executable in [
        python.to_owned(),
        python.canonicalize().map_err(|e| e.to_string())?,
    ] {
        if let Some(bin) = executable
            .parent()
            .filter(|bin| bin.file_name().is_some_and(|name| name == "bin"))
        {
            let prefix = bin.parent().expect("bin directory has a parent");
            for name in ["lib", "lib64", "pyvenv.cfg"] {
                let path = prefix.join(name);
                if std::fs::symlink_metadata(&path)
                    .is_ok_and(|metadata| !metadata.file_type().is_symlink())
                {
                    paths.push(path);
                }
            }
        }
        paths.push(executable);
    }
    Ok(paths)
}

pub(crate) fn protect_worker(
    policy: &mut crate::settings::SandboxSettings,
    workspace: &Path,
    protected: &[PathBuf],
) -> Result<(), String> {
    // Accept the native constructors we can augment without retaining custom
    // more-specific write rules. Native code still owns path semantics.
    let mut baseline = crate::settings::SandboxSettings::new();
    if let Some(profile) = policy.get("extends") {
        baseline.insert("extends".into(), profile.clone());
    }
    let mut baseline = crate::sandbox::materialize_settings(baseline, Vec::new(), workspace)?;
    let mut actual = policy.clone();
    for key in ["network", "proxy", "environment", "inherit_environment"] {
        baseline.remove(key);
        actual.remove(key);
    }
    if actual != baseline {
        return Err("managed resolver storage requires the default, :workspace, or :read-only worker filesystem policy without custom filesystem rules or native extensions".into());
    }
    let filesystem = policy
        .entry("filesystem")
        .or_insert_with(|| json!({"kind":"restricted","entries":[]}));
    let entries = filesystem
        .as_object_mut()
        .ok_or("worker filesystem must be an object")?
        .entry("entries")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or("worker entries must be an array")?;
    for path in protected {
        if workspace.starts_with(path) {
            return Err("worker workspace overlaps protected resolver or launch storage".into());
        }
        entries.push(json!({"path":{"type":"path","path":path},"access":"read"}));
    }
    Ok(())
}
