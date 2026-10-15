//! Offline client configuration edits. Client trust and tool approvals remain client-owned.

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use serde_json::json;
use toml_edit::{Array, DocumentMut, Item, Table, value};

use crate::cli::{InstallClient, InstallOptions, InstallScope};

pub(crate) fn run(options: &InstallOptions) -> Result<String, String> {
    let directory = std::env::current_dir().map_err(|error| error.to_string())?;
    let git_root = directory
        .ancestors()
        .find(|ancestor| ancestor.join(".git").exists());
    let project = match options.scope {
        InstallScope::Auto => git_root,
        InstallScope::Project => Some(git_root.unwrap_or(&directory)),
        InstallScope::Global => None,
    };
    let (path, scope) = match project {
        Some(project) => {
            let path = match options.client {
                InstallClient::Codex => project.join(".codex").join("config.toml"),
                InstallClient::Claude => project.join(".mcp.json"),
            };
            (path, "project")
        }
        None => (global_path(options.client, &directory)?, "global"),
    };
    let previous = match fs::read_to_string(&path) {
        Ok(source) => Some(source),
        Err(error) if error.kind() == ErrorKind::NotFound => None,
        Err(error) => return Err(format!("Cannot read {}: {error}", path.display())),
    };
    let auto_load = !options.no_auto_load;
    let (updated, snippet) = match options.client {
        InstallClient::Codex => edit_codex(previous.as_deref(), auto_load),
        InstallClient::Claude => edit_claude(previous.as_deref(), auto_load),
    }
    .map_err(|error| format!("Cannot configure {}: {error}", path.display()))?;
    let changed = previous.as_deref() != Some(updated.as_str());
    if changed && !options.dry_run {
        write_configuration(&path, &updated)
            .map_err(|error| format!("Cannot write {}: {error}", path.display()))?;
    }
    let action = if options.dry_run {
        "Would configure"
    } else if changed {
        "Configured"
    } else {
        "Already configured"
    };
    let client = match options.client {
        InstallClient::Codex => "Codex",
        InstallClient::Claude => "Claude Code",
    };
    let mut output = format!(
        "{action} Console for {client} ({scope}).\nConfiguration: {}\n\nSet these fields:\n{snippet}\n",
        path.display()
    );
    if auto_load {
        output.push_str("Console tools are configured to load up front. Use --no-auto-load to keep the client's default tool discovery.\n");
    } else {
        output.push_str("Console tools use the client's default tool discovery.\n");
    }
    if options.dry_run {
        output.push_str("Dry run; no files changed. You can apply the settings above manually.\n");
    } else {
        output.push_str(&format!("Restart {client} to load the configuration.\n"));
    }
    if project.is_some() {
        output.push_str(match options.client {
            InstallClient::Codex => {
                "Codex loads project configuration only after you trust the project.\n"
            }
            InstallClient::Claude => "Approve the project server in Claude Code if prompted.\n",
        });
    }
    Ok(output)
}

fn global_path(client: InstallClient, directory: &Path) -> Result<PathBuf, String> {
    let variable = match client {
        InstallClient::Codex => "CODEX_HOME",
        InstallClient::Claude => "CLAUDE_CONFIG_DIR",
    };
    let home = match std::env::var_os(variable).filter(|value| !value.is_empty()) {
        Some(home) => PathBuf::from(home),
        None => {
            let home = std::env::home_dir().ok_or("Cannot determine the user home directory")?;
            match client {
                InstallClient::Codex => home.join(".codex"),
                InstallClient::Claude => home,
            }
        }
    };
    let filename = match client {
        InstallClient::Codex => "config.toml",
        InstallClient::Claude => ".claude.json",
    };
    Ok(directory.join(home).join(filename))
}

fn edit_codex(source: Option<&str>, auto_load: bool) -> Result<(String, String), String> {
    let mut document = source
        .unwrap_or("")
        .parse::<DocumentMut>()
        .map_err(|error| error.to_string())?;
    let mut parent = Table::new();
    parent.set_implicit(true);
    let servers = document
        .entry("mcp_servers")
        .or_insert(Item::Table(parent))
        .as_table_like_mut()
        .ok_or("mcp_servers must be a table")?;
    let console = servers
        .entry("console")
        .or_insert(Item::Table(Table::new()))
        .as_table_like_mut()
        .ok_or("mcp_servers.console must be a table")?;
    if console.contains_key("url") {
        return Err("mcp_servers.console is a remote server; remove or rename it before installing the stdio server".into());
    }
    let mut desired = Table::new();
    desired.insert("command", value("uvx"));
    desired.insert(
        "args",
        value(["mcp-console", "serve"].into_iter().collect::<Array>()),
    );
    desired.insert("enabled", value(true));
    // This per-server policy keeps tools directly exposed without disabling
    // tool search for other servers. Strict Code Mode Only overrides it.
    desired.insert(
        "omit_tools_from",
        value(if auto_load {
            ["deferred"].into_iter().collect::<Array>()
        } else {
            Array::new()
        }),
    );
    for (key, item) in desired.iter() {
        console.insert(key, item.clone());
    }
    Ok((
        document.to_string(),
        format!("[mcp_servers.console]\n{desired}"),
    ))
}

fn edit_claude(source: Option<&str>, auto_load: bool) -> Result<(String, String), String> {
    let mut document: serde_json::Value = match source {
        Some(source) => serde_json::from_str(source).map_err(|error| error.to_string())?,
        None => json!({}),
    };
    let root = document
        .as_object_mut()
        .ok_or("configuration must be a JSON object")?;
    let servers = root
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("mcpServers must be a JSON object")?;
    let console = servers
        .entry("console")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("mcpServers.console must be a JSON object")?;
    if console.contains_key("url") || console.get("type").is_some_and(|kind| kind != "stdio") {
        return Err(
            "mcpServers.console is not a stdio server; remove or rename it before installing"
                .into(),
        );
    }
    let desired = json!({
        "type": "stdio",
        "command": "uvx",
        "args": ["mcp-console", "serve"],
        "alwaysLoad": auto_load,
    });
    console.extend(desired.as_object().unwrap().clone());
    let snippet = serde_json::to_string_pretty(&json!({"mcpServers": {"console": desired}}))
        .map_err(|error| error.to_string())?;
    let updated =
        serde_json::to_string_pretty(&document).map_err(|error| error.to_string())? + "\n";
    Ok((updated, snippet + "\n"))
}

fn write_configuration(path: &Path, source: &str) -> Result<(), std::io::Error> {
    // Follow dotfile symlinks rather than replacing the link. Broken links fail
    // explicitly. Configuration edits assume no concurrent client/installer writer.
    let (target, permissions) = match fs::symlink_metadata(path) {
        Ok(_) => (
            path.canonicalize()?,
            Some(fs::metadata(path)?.permissions()),
        ),
        Err(error) if error.kind() == ErrorKind::NotFound => (path.to_path_buf(), None),
        Err(error) => return Err(error),
    };
    let parent = target.parent().expect("configuration path is absolute");
    fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(source.as_bytes())?;
    if let Some(permissions) = permissions {
        file.as_file().set_permissions(permissions)?;
    }
    file.persist(target).map_err(|error| error.error)?;
    Ok(())
}
