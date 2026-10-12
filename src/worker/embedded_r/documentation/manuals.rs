//! Installed HTML -> one Markdown document and navigation index per manual.
//! Only the captured manuals subtree is writable. A content-addressed directory
//! is published by rename after every projection and the manifest are complete.

use crate::resolver::cache::restore_directory_access;
use anyhow::{Context, Result, bail, ensure};
use harp::object::RObject;
use html2text::render::{RichAnnotation, TaggedLineElement};
use html2text::{Handle, RcDom};
use libr::SEXP;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

// Bump when conversion or its derived navigation index changes.
const FORMAT: u32 = 8;
const SEARCH_BUDGET: usize = 4096;
const READ_BUDGET: usize = 5000; // Paths, commands, and notices fit below 8 KiB.
const READ_BODY_BUDGET: usize = 4500; // Ordinary host paths do not change continuations.

#[derive(Serialize)]
struct Collection {
    format: u32,
    installation: String,
    r_version: String,
    source_directory: String,
    sources: Vec<Source>,
    manuals: Vec<Manual>,
}

#[derive(Serialize)]
struct Source {
    name: String,
    path: String,
    sha256: String,
}

#[derive(Serialize, Default)]
struct Manual {
    name: String,
    title: String,
    sections: Vec<Section>,
    // The manifest records navigation metadata, never a second body copy.
    #[serde(skip)]
    blocks: Vec<Block>,
    #[serde(skip)]
    positions: Vec<std::ops::Range<usize>>,
    anchors: BTreeMap<String, Anchor>,
    toc_anchors: Vec<String>,
}

#[derive(Serialize)]
struct Section {
    title: String,
    level: usize,
    parent: Option<usize>,
    // Inclusive, one-based Markdown line ranges, including subsections.
    start: usize,
    end: usize,
}

#[derive(Serialize)]
struct Anchor {
    line: usize,
    section: Option<usize>,
}

struct Block {
    section: usize,
    heading: bool,
    anchors: Vec<String>,
    row_anchors: Vec<String>,
    text: String,
    links: Vec<String>,
}

impl Block {
    fn all_anchors(&self) -> impl Iterator<Item = &String> {
        self.anchors.iter().chain(&self.row_anchors)
    }

    fn visible(&self, text: &str) -> String {
        let mut text = text.to_string();
        for anchor in &self.row_anchors {
            text = text.replace(&anchor_html(anchor), "");
        }
        text
    }
}

type Files = BTreeMap<String, String>;

#[derive(Clone)]
struct Prepared {
    identity: String,
    collection: Arc<Collection>,
    files: Arc<Files>,
}

// Submitted code can write cache data. Authenticate it from installed sources,
// never from the cached manifest; retain that source-derived model per worker.
static PREPARED: Mutex<Option<Prepared>> = Mutex::new(None);

fn attribute(node: &Handle, key: &str) -> Option<String> {
    if let html2text::Element { attrs, .. } = &node.data {
        attrs
            .borrow()
            .iter()
            .find(|a| a.name.local.as_ref() == key)
            .map(|a| a.value.to_string())
    } else {
        None
    }
}

fn tag(node: &Handle) -> &str {
    match &node.data {
        html2text::Element { name, .. } => name.local.as_ref(),
        _ => "",
    }
}

fn anchors(node: &Handle) -> Vec<String> {
    let mut found = attribute(node, "id").into_iter().collect::<Vec<_>>();
    if tag(node) == "a"
        && let Some(name) = attribute(node, "name")
    {
        found.push(name);
    }
    for child in node.children.borrow().iter() {
        found.extend(anchors(child));
    }
    found
}

fn plain(node: &Handle, width: usize) -> Result<String> {
    let dom = RcDom::default();
    dom.document.children.borrow_mut().push(node.clone());
    let config = html2text::config::plain_no_decorate().allow_width_overflow();
    Ok(config
        .render_to_string(config.dom_to_render_tree(&dom)?, width)?
        .trim_end()
        .into())
}

// Link destinations remain typed slots until the complete collection has an
// anchor map. This avoids parsing renderer-specific heading slugs or Markdown.
fn inline(node: &Handle, links: &mut Vec<String>, width: usize) -> Result<(String, String)> {
    let dom = RcDom::default();
    if matches!(tag(node), "td" | "th") {
        // html2text renders cells only inside a table. Render their inline
        // children here; the table projection owns the Markdown row structure.
        dom.document
            .children
            .borrow_mut()
            .extend(node.children.borrow().iter().cloned());
    } else {
        dom.document.children.borrow_mut().push(node.clone());
    }
    let config = html2text::config::rich().allow_width_overflow();
    let lines = config.render_to_lines(config.dom_to_render_tree(&dom)?, width)?;
    let mut output = String::new();
    let mut label = String::new();
    for line in lines {
        for item in line.iter() {
            if let TaggedLineElement::Str(span) = item {
                label.push_str(&span.s);
                let mut text = span.s.clone();
                if span.tag.contains(&RichAnnotation::Code) {
                    let fence = "`".repeat(text.chars().filter(|c| *c == '`').count() + 1);
                    text = if text.contains('`') {
                        format!("{fence} {text} {fence}")
                    } else {
                        format!("{fence}{text}{fence}")
                    };
                }
                if span.tag.contains(&RichAnnotation::Strong) {
                    text = format!("**{text}**");
                }
                if span.tag.contains(&RichAnnotation::Emphasis) {
                    text = format!("*{text}*");
                }
                if let Some(RichAnnotation::Link(url)) = span
                    .tag
                    .iter()
                    .find(|a| matches!(a, RichAnnotation::Link(_)))
                {
                    let slot = links.len();
                    links.push(url.clone());
                    text = format!("[{text}](\u{1f}{slot}\u{1f})");
                }
                output.push_str(&text);
            }
        }
        output.push('\n');
        label.push('\n');
    }
    Ok((output.trim_end().into(), label.trim_end().into()))
}

fn descendants(node: &Handle, names: &[&str], output: &mut Vec<Handle>) {
    if names.contains(&tag(node)) {
        output.push(node.clone());
    } else {
        for child in node.children.borrow().iter() {
            descendants(child, names, output);
        }
    }
}

fn remove_self_links(node: &Handle) {
    node.children.borrow_mut().retain(|child| {
        !attribute(child, "class")
            .is_some_and(|class| class.split_whitespace().any(|c| c == "copiable-link"))
    });
    for child in node.children.borrow().iter() {
        remove_self_links(child);
    }
}

fn table(node: &Handle, links: &mut Vec<String>) -> Result<(String, Vec<String>)> {
    let mut rows = Vec::new();
    descendants(node, &["tr"], &mut rows);
    let mut rendered = Vec::new();
    let mut row_anchors = Vec::new();
    for row in rows {
        let row_ids = anchors(&row);
        let mut cells = Vec::new();
        descendants(&row, &["td", "th"], &mut cells);
        let mut row = cells
            .iter()
            .map(|cell| {
                Ok(inline(cell, links, 100_000)?
                    .0
                    .replace('|', "\\|")
                    .replace('\n', "<br>"))
            })
            .collect::<Result<Vec<_>>>()?;
        if let Some(first) = row.first_mut() {
            // Keep an index letter or entry beside its original row, while
            // preserving the entire table as one indivisible body block.
            let markers = row_ids.iter().map(|id| anchor_html(id)).collect::<String>();
            first.insert_str(0, &markers);
            row_anchors.extend(row_ids);
        }
        rendered.push(row);
    }
    let columns = rendered.iter().map(Vec::len).max().unwrap_or(0);
    let mut text = String::new();
    for (i, mut row) in rendered.into_iter().enumerate() {
        row.resize(columns, String::new());
        text.push_str(&format!("| {} |\n", row.join(" | ")));
        if i == 0 {
            text.push_str(&format!("| {} |\n", vec!["---"; columns].join(" | ")));
        }
    }
    Ok((text.trim_end().into(), row_anchors))
}

fn walk(node: &Handle, manual: &mut Manual, pending: &mut Vec<String>) -> Result<()> {
    let name = tag(node);
    let class = attribute(node, "class").unwrap_or_default();
    if matches!(name, "head" | "script" | "style") {
        return Ok(());
    }
    if class.split_whitespace().any(|c| {
        matches!(
            c,
            "nav-panel" | "copiable-link" | "region-contents" | "contents"
        )
    }) {
        manual.toc_anchors.extend(anchors(node));
        return Ok(());
    }
    if let Some(id) = attribute(node, "id") {
        pending.push(id);
    }
    if name == "a"
        && let Some(id) = attribute(node, "name")
    {
        pending.push(id);
    }
    if let Some(level) = name.strip_prefix('h').and_then(|n| n.parse::<usize>().ok())
        && (1..=6).contains(&level)
    {
        // Self-link glyphs are navigation, not heading content.
        pending.extend(anchors(node));
        remove_self_links(node);
        // html2text consumes child handles. Obtain the visible heading and its
        // Markdown links in one pass rather than rendering the node twice.
        let mut links = Vec::new();
        let (heading, title) = inline(node, &mut links, 100_000)?;
        let title = title
            .trim_start_matches('#')
            .trim()
            .trim_end_matches('¶')
            .trim()
            .to_string();
        let parent = manual
            .sections
            .iter()
            .rposition(|section| section.level < level);
        let section = manual.sections.len();
        manual.sections.push(Section {
            title: title.clone(),
            level,
            parent,
            start: 0,
            end: 0,
        });
        if manual.title.is_empty() {
            manual.title = title.clone();
        }
        pending.sort();
        pending.dedup();
        if pending.is_empty() {
            // Cache-local identity for headings such as installed Footnotes
            // which have no original HTML anchor. Original IDs stay unchanged.
            pending.push(format!("console-section-{section}"));
        }
        manual.blocks.push(Block {
            section,
            heading: true,
            anchors: std::mem::take(pending),
            row_anchors: Vec::new(),
            text: format!(
                "{} {}",
                "#".repeat(level),
                heading.trim_start_matches('#').trim()
            ),
            links,
        });
    } else if matches!(
        name,
        "p" | "pre" | "table" | "ul" | "ol" | "dl" | "blockquote" | "caption"
    ) {
        let section = manual
            .sections
            .len()
            .checked_sub(1)
            .context("manual content must have a heading")?;
        pending.extend(anchors(node));
        pending.sort();
        pending.dedup();
        let mut links = Vec::new();
        let mut row_anchors = Vec::new();
        let text = match name {
            "pre" => {
                let body = plain(node, 100_000)?;
                let fence = "`".repeat(
                    body.split(|c| c != '`')
                        .map(str::len)
                        .max()
                        .unwrap_or(0)
                        .max(2)
                        + 1,
                );
                format!("{fence}\n{body}\n{fence}")
            }
            "table" => {
                let (text, embedded) = table(node, &mut links)?;
                pending.retain(|anchor| !embedded.contains(anchor));
                row_anchors = embedded;
                text
            }
            _ => inline(node, &mut links, 80)?.0,
        };
        manual.blocks.push(Block {
            section,
            heading: false,
            anchors: std::mem::take(pending),
            row_anchors,
            text,
            links,
        });
    } else {
        for child in node.children.borrow().iter() {
            walk(child, manual, pending)?;
        }
        // Standalone text (including Texinfo footnote labels) must survive.
        // html2text does not export its Text variant. Serialization distinguishes
        // text from declarations without depending on the crate's private enum.
        if name.is_empty() && !matches!(&node.data, html2text::Document | html2text::Comment { .. })
        {
            let dom = RcDom::default();
            dom.document.children.borrow_mut().push(node.clone());
            let mut bytes = Vec::new();
            dom.serialize(&mut bytes)?;
            let raw = std::str::from_utf8(&bytes)?.trim();
            let text = if raw.is_empty() || raw.starts_with('<') {
                String::new()
            } else {
                plain(node, 80)?.trim().to_string()
            };
            if !text.is_empty()
                && let Some(section) = manual.sections.len().checked_sub(1)
            {
                manual.blocks.push(Block {
                    section,
                    heading: false,
                    anchors: std::mem::take(pending),
                    row_anchors: Vec::new(),
                    text,
                    links: Vec::new(),
                });
            }
        }
    }
    Ok(())
}

fn parse(name: &str, bytes: &[u8]) -> Result<Manual> {
    std::str::from_utf8(bytes).context("installed manual HTML must be UTF-8")?;
    let dom = html2text::config::plain().parse_html(bytes)?;
    super::lift_captions(&dom);
    let mut manual = Manual {
        name: name.into(),
        ..Default::default()
    };
    let mut pending = Vec::new();
    walk(&dom.document, &mut manual, &mut pending)?;
    ensure!(
        !manual.blocks.is_empty(),
        "installed manual has no content: {name}"
    );
    manual.blocks.last_mut().unwrap().anchors.extend(pending);
    for block in &manual.blocks {
        for anchor in block.all_anchors() {
            manual.anchors.insert(
                anchor.clone(),
                Anchor {
                    line: 0,
                    section: Some(block.section),
                },
            );
        }
    }
    for anchor in &manual.toc_anchors {
        manual.anchors.insert(
            anchor.clone(),
            Anchor {
                line: 0,
                section: None,
            },
        );
    }
    Ok(manual)
}

fn anchor_html(anchor: &str) -> String {
    format!(
        "<a id=\"{}\"></a>",
        anchor
            .replace('&', "&amp;")
            .replace('"', "&quot;")
            .replace('<', "&lt;")
    )
}

fn location(collection: &Collection, current: &Manual, url: &str) -> String {
    if url.contains(':') || url.starts_with("//") {
        return url.into();
    }
    let (file, anchor) = url.split_once('#').unwrap_or((url, ""));
    let name = if file.is_empty() {
        current.name.as_str()
    } else {
        file.strip_suffix(".html").unwrap_or(file)
    };
    if let Some(manual) = collection.manuals.iter().find(|m| m.name == name)
        && (anchor.is_empty() || manual.anchors.contains_key(anchor))
    {
        let file = if name == current.name && !anchor.is_empty() {
            String::new()
        } else {
            format!("{name}.md")
        };
        return if anchor.is_empty() {
            file
        } else {
            format!("{file}#{anchor}")
        };
    }
    format!("unavailable local target: {url}")
}

fn project(collection: &Collection, manual: &Manual, block: &Block) -> String {
    let mut text = block.text.clone();
    for (i, link) in block.links.iter().enumerate() {
        let destination = location(collection, manual, link);
        text = text.replace(&format!("\u{1f}{i}\u{1f}"), &destination);
    }
    // Unavailable local destinations remain readable, without a broken link.
    text = text.replace(
        "](unavailable local target: ",
        "] [unavailable local target: ",
    );
    for link in block
        .links
        .iter()
        .filter(|link| location(collection, manual, link).starts_with("unavailable local target: "))
    {
        text = text.replace(&format!("{link})"), &format!("{link}]"));
    }
    let anchors = block
        .anchors
        .iter()
        .map(|a| anchor_html(a))
        .collect::<Vec<_>>()
        .join("\n");
    if anchors.is_empty() {
        format!("{text}\n\n")
    } else {
        format!("{anchors}\n{text}\n\n")
    }
}

fn heading_path(manual: &Manual, mut section: usize) -> String {
    let mut path = Vec::new();
    loop {
        path.push(manual.sections[section].title.as_str());
        if let Some(parent) = manual.sections[section].parent {
            section = parent;
        } else {
            break;
        }
    }
    path.reverse();
    path.join(" > ")
}

fn section_anchor(manual: &Manual, section: usize) -> &str {
    manual
        .blocks
        .iter()
        .find(|block| block.heading && block.section == section)
        .unwrap()
        .anchors
        .first()
        .unwrap()
}

fn toc(manual: &Manual, compact: bool) -> String {
    // The file's heading tree contains only source headings; its generated TOC
    // must not introduce a second manual title for ordinary Markdown queries.
    let title = if compact {
        format!("# {}", manual.title)
    } else {
        format!("**Contents: {}**", manual.title)
    };
    let mut text = format!("{title}\n\n[Collection](index.md)\n\n");
    for (i, section) in manual.sections.iter().enumerate() {
        if compact && section.level > 2 {
            continue;
        }
        text.push_str(&format!(
            "{}- [{}]({}.md#{})\n",
            "  ".repeat(section.level.saturating_sub(1)),
            section.title,
            manual.name,
            section_anchor(manual, i)
        ));
    }
    if compact {
        text.push_str(&format!("\nRead: `RShowDoc(\"{}#original-anchor\")`\nSearch: `.console$manual_search(\"phrase\", manual = \"{}\")`\nFull TOC and document: `.console$manual(\"{}.md\")`\n", manual.name, manual.name, manual.name));
    }
    text
}

fn fingerprint(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn projected_files(collection: &mut Collection) -> Result<Files> {
    let mut files = Files::new();
    let mut index = format!("# Installed R manuals\n\n{}\n\n", collection.r_version);
    // Resolve links using the complete original-anchor inventory, then record
    // line ranges in the rendered document. Queries never reconstruct chunks.
    let documents = collection
        .manuals
        .iter()
        .map(|manual| {
            let mut text = format!(
                "<!-- Installed {}, {}. Converted under the permission notice below. -->\n\n",
                manual.name, collection.r_version
            );
            let mut anchors = BTreeMap::new();
            let mut line = text.lines().count();
            for anchor in &manual.toc_anchors {
                anchors.insert(anchor.clone(), line + 1);
                text.push_str(&format!("{}\n", anchor_html(anchor)));
                line += 1;
            }
            let contents = toc(manual, false);
            line += contents.lines().count();
            text.push_str(&contents);
            text.push('\n');
            line += 1;
            let mut positions = Vec::new();
            for block in &manual.blocks {
                let rendered = project(collection, manual, block);
                for anchor in block.all_anchors() {
                    let offset = rendered
                        .lines()
                        .position(|text| text.contains(&anchor_html(anchor)))
                        .unwrap();
                    anchors.insert(anchor.clone(), line + offset + 1);
                }
                let end = line + rendered.lines().count();
                positions.push(line..end);
                line = end;
                text.push_str(&rendered);
            }
            (text, positions, anchors)
        })
        .collect::<Vec<_>>();
    for (manual, (text, positions, anchors)) in collection.manuals.iter_mut().zip(documents) {
        for (anchor, line) in anchors {
            manual.anchors.get_mut(&anchor).unwrap().line = line;
        }
        for block in manual
            .blocks
            .iter()
            .enumerate()
            .filter(|(_, block)| block.heading)
        {
            manual.sections[block.1.section].start = positions[block.0].start + 1;
        }
        for i in 0..manual.sections.len() {
            manual.sections[i].end = manual.sections[i + 1..]
                .iter()
                .find(|section| section.level <= manual.sections[i].level)
                .map_or(text.lines().count(), |section| section.start - 1);
        }
        manual.positions = positions;
        index.push_str(&format!(
            "- [{} ({})]({}.md)\n",
            manual.title, manual.name, manual.name
        ));
        files.insert(format!("{}.md", manual.name), text);
    }
    index.push_str("\nRead: `RShowDoc(\"R-exts\")`\nSearch: `.console$manual_search(\"phrase\")`\n\nEach manual has one complete Markdown file. The manifest maps original anchors and section ranges into those same files.\n");
    files.insert("index.md".into(), index);
    files.insert(
        "manifest.json".into(),
        serde_json::to_string_pretty(collection)?,
    );
    Ok(files)
}

fn matches_file(path: &Path, expected: &str) -> std::io::Result<bool> {
    let mut options = fs::File::options();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() != expected.len() as u64 {
        return Ok(false);
    }
    // Bound the read as well: submitted code can grow a file after metadata.
    let mut actual = Vec::new();
    file.take(expected.len() as u64 + 1)
        .read_to_end(&mut actual)?;
    Ok(actual == expected.as_bytes())
}

fn remove_entry(path: &Path) -> std::io::Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    // Repair directory traversal before descent; never follow cache symlinks.
    if metadata.is_dir() {
        restore_directory_access(path, &metadata)?;
        for entry in fs::read_dir(path)? {
            remove_entry(&entry?.path())?;
        }
        fs::remove_dir(path)
    } else {
        #[cfg(windows)]
        // Clearing the Windows read-only attribute leaves ACL permissions intact.
        #[allow(clippy::permissions_set_readonly_false)]
        if metadata.is_file() && metadata.permissions().readonly() {
            let mut permissions = metadata.permissions();
            permissions.set_readonly(false);
            fs::set_permissions(path, permissions)?;
        }
        fs::remove_file(path)
    }
}

fn complete(directory: &Path, prepared: &Prepared) -> std::io::Result<bool> {
    if !fs::symlink_metadata(directory)?.is_dir() {
        return Ok(false);
    }
    let mut expected = prepared
        .files
        .iter()
        .map(|(name, text)| (PathBuf::from(name), text))
        .collect::<BTreeMap<_, _>>();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        // A complete collection is flat. Reject links, special files, directories,
        // and unexpected files before reading any worker-writable contents.
        if !entry.file_type()?.is_file() {
            return Ok(false);
        }
        let Some(text) = expected.remove(Path::new(&entry.file_name())) else {
            return Ok(false);
        };
        if !matches_file(&entry.path(), text)? {
            return Ok(false);
        }
    }
    Ok(expected.is_empty())
}

fn materialize(args: &[String]) -> Result<(PathBuf, Prepared, fs::File)> {
    let root = std::env::var_os("MCP_CONSOLE_MANUAL_CACHE")
        .filter(|s| !s.is_empty())
        .context("Installed manual cache is unavailable; check the host cache location")?;
    let source =
        fs::canonicalize(&args[1]).context("Installed HTML manual directory is unavailable")?;
    let mut paths = fs::read_dir(&source)?
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.retain(|p| {
        p.extension().is_some_and(|ext| ext == "html")
            && p.file_stem()
                .is_some_and(|n| n.to_string_lossy().starts_with("R-"))
    });
    paths.sort();
    ensure!(
        !paths.is_empty(),
        "No installed HTML R manuals in {}",
        source.display()
    );
    let mut collection = Collection {
        format: FORMAT,
        installation: args[0].clone(),
        source_directory: source.to_string_lossy().into(),
        r_version: args[2].clone(),
        sources: Vec::new(),
        manuals: Vec::new(),
    };
    let mut inputs = Vec::new();
    for path in paths {
        let bytes = fs::read(&path)?;
        let name = path
            .file_stem()
            .unwrap()
            .to_str()
            .context("manual filenames must be UTF-8")?
            .to_string();
        collection.sources.push(Source {
            name: name.clone(),
            path: path.to_string_lossy().into(),
            sha256: fingerprint(&bytes),
        });
        inputs.push((name, bytes));
    }
    let identity = fingerprint(&serde_json::to_vec(&collection)?);
    let root = PathBuf::from(root);
    let directory = root.join(&identity);
    let prepared = {
        let mut cached = PREPARED.lock().expect("manual source model lock");
        if cached
            .as_ref()
            .is_none_or(|entry| entry.identity != identity)
        {
            for (name, bytes) in inputs {
                collection.manuals.push(parse(&name, &bytes)?);
            }
            let files = Arc::new(projected_files(&mut collection)?);
            *cached = Some(Prepared {
                identity,
                collection: Arc::new(collection),
                files,
            });
        }
        cached.as_ref().unwrap().clone()
    };
    // The host creates this lock outside the worker-writable manuals subtree.
    // Workers need only read access to lock it and cannot replace its inode.
    let lock = fs::File::open(root.with_extension("lock"))?;
    // Shell-capable peers can take an advisory lock. Never let contention
    // block the interpreter; return an explicit retry error before cache access.
    match lock.try_lock() {
        Ok(()) => {}
        Err(fs::TryLockError::WouldBlock) => {
            bail!("Manual cache is busy; retry the manual operation")
        }
        Err(fs::TryLockError::Error(error)) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(&root)?;
    ensure!(metadata.is_dir(), "Manual cache root is not a directory");
    restore_directory_access(&root, &metadata)?;
    let staging = root.join(format!(".{}.incomplete", prepared.identity));
    // A retired publisher may leave staging behind. The shared lock proves no
    // publisher is still using this identity-scoped directory, even on reuse.
    remove_entry(&staging)?;
    let complete = || complete(&directory, &prepared).unwrap_or(false);
    if complete() {
        return Ok((directory, prepared, lock));
    }
    fs::create_dir(&staging)?;
    for (name, text) in prepared.files.iter() {
        let path = staging.join(name);
        fs::create_dir_all(path.parent().unwrap())?;
        fs::write(path, text)?;
    }
    remove_entry(&directory)?;
    fs::rename(&staging, &directory)?;
    Ok((directory, prepared, lock))
}

fn read(
    collection: &Collection,
    files: &Files,
    directory: &Path,
    args: &[String],
) -> Result<Vec<String>> {
    let topic = &args[4];
    let (name, anchor) = topic.split_once('#').unwrap_or((topic, ""));
    let manual = collection
        .manuals
        .iter()
        .find(|manual| manual.name == name.trim_end_matches(".md"));
    ensure!(
        (topic.is_empty() || topic == "index.md") && anchor.is_empty() || manual.is_some(),
        "Unknown installed manual or content file: {topic}"
    );
    let relative = manual.map_or_else(|| "index.md".into(), |manual| format!("{}.md", manual.name));
    let compact = manual.is_some_and(|manual| topic == &manual.name);
    let text = if compact {
        toc(manual.unwrap(), true)
    } else {
        files[&relative].clone()
    };
    let lines = text.lines().collect::<Vec<_>>();
    let mut start = args[6].parse::<usize>()? - 1;
    let column = args[8].parse::<usize>()? - 1;
    let mut end = lines.len();
    let mut section = None;
    if !anchor.is_empty() {
        let manual = manual.unwrap();
        let location = manual
            .anchors
            .get(anchor)
            .with_context(|| format!("Unknown manual anchor: {topic}"))?;
        if start == 0 && column == 0 {
            start = location.line - 1;
        }
        section = location.section;
        end = location
            .section
            .map_or(manual.positions[0].start, |section| {
                manual.sections[section].end
            });
        ensure!(
            start >= location.line - 1,
            "Line range begins before the requested anchor"
        );
    } else if !compact && let Some(manual) = manual {
        section = manual
            .sections
            .iter()
            .rposition(|section| section.start <= start + 1);
    }
    ensure!(start < end, "Line range begins past the end ({end} lines)");
    let requested = args[7].parse::<usize>()?;
    let range_end = if requested == 0 {
        end
    } else {
        (start + requested).min(end)
    };
    let mut output = format!("Cache: {}\nFile: {relative}\n", directory.display());
    if let Some(section) = section {
        output.push_str(&format!(
            "Location: {}\n",
            heading_path(manual.unwrap(), section)
        ));
    }
    output.push('\n');
    let body_start = output.len();
    // A preview may begin or end inside an oversized code block. Add only
    // display fences; continuation coordinates still refer to the document.
    let fence_at = |line: usize| {
        manual.filter(|_| !compact).and_then(|manual| {
            manual
                .blocks
                .iter()
                .zip(&manual.positions)
                .find_map(|(block, range)| {
                    let fence = block.text.lines().next()?;
                    if fence.len() < 3 || !fence.chars().all(|c| c == '`') {
                        return None;
                    }
                    let opening = range.start + block.anchors.len();
                    let closing = opening + block.text.lines().count() - 1;
                    (opening < line && line <= closing).then_some(fence)
                })
        })
    };
    if let Some(fence) = fence_at(start) {
        output.push_str(fence);
        output.push('\n');
    }
    let budget = output.len() + READ_BODY_BUDGET.min(READ_BUDGET.saturating_sub(output.len()));
    let mut next = start;
    let mut next_column = column;
    let mut heading_start = None;
    for (i, line) in lines.iter().enumerate().take(range_end).skip(start) {
        if requested == 0
            && !compact
            && let Some(manual) = manual
            && let Some((block, range)) = manual
                .blocks
                .iter()
                .zip(&manual.positions)
                .find(|(_, range)| range.start == i)
        {
            if block.heading {
                heading_start = Some((i, output.len()));
            } else {
                let bytes = lines[range.clone()]
                    .iter()
                    .map(|line| line.len() + 1)
                    .sum::<usize>();
                // Keep ordinary blocks whole once the page has useful context.
                // Oversized examples still use explicit line/column continuation.
                if bytes <= READ_BODY_BUDGET
                    && bytes > budget.saturating_sub(output.len())
                    && output.len() - body_start >= READ_BODY_BUDGET / 2
                {
                    if let Some((line, length)) = heading_start {
                        output.truncate(length);
                        next = line;
                    } else {
                        next = i;
                    }
                    next_column = 0;
                    break;
                }
                heading_start = None;
            }
        }
        let mut visible = (*line).to_string();
        if !compact && let Some(manual) = manual {
            for (id, location) in &manual.anchors {
                if location.line == i + 1 {
                    visible = visible.replace(&anchor_html(id), "");
                }
            }
            if visible.trim().is_empty() && !line.trim().is_empty() {
                next = i + 1;
                next_column = 0;
                continue;
            }
        }
        let part = if i == start {
            visible.chars().skip(column).collect::<String>()
        } else {
            visible
        };
        let available = budget.saturating_sub(output.len());
        if part.len() + 1 > available {
            if i == start {
                let mut used = 0;
                let prefix = part
                    .chars()
                    .take_while(|c| {
                        used += c.len_utf8();
                        used <= available
                    })
                    .collect::<String>();
                next_column += prefix.chars().count();
                output.push_str(&prefix);
                output.push('\n');
            }
            break;
        }
        output.push_str(&part);
        output.push('\n');
        next = i + 1;
        next_column = 0;
    }
    if let Some(fence) = fence_at(next) {
        output.push_str(fence);
        output.push('\n');
    }
    if next < end {
        output.push_str(&format!(
            "\nContinue: .console$manual({topic:?}, start = {}, column = {})\n",
            next + 1,
            next_column + 1
        ));
    } else if !anchor.is_empty()
        && let Some(manual) = manual
        && let Some(next_section) = manual
            .sections
            .iter()
            .position(|section| section.start == end + 1)
    {
        output.push_str(&format!(
            "\nNext section: RShowDoc({:?})\n",
            format!("{}#{}", manual.name, section_anchor(manual, next_section))
        ));
    }
    Ok(vec![
        directory.join(relative).to_string_lossy().into(),
        output,
    ])
}

// Match rendered prose independently of line wrapping, retaining coordinates
// in the original document for exact reading commands and Unicode case changes.
fn matching_offset(text: &str, query: &str) -> Option<usize> {
    let mut normalized = String::new();
    let mut offsets = Vec::new();
    for (offset, character) in text.char_indices() {
        if character.is_whitespace() {
            if !normalized.is_empty() && !normalized.ends_with(' ') {
                normalized.push(' ');
                offsets.push(offset);
            }
        } else {
            for character in character.to_lowercase() {
                normalized.push(character);
                offsets.extend(std::iter::repeat_n(offset, character.len_utf8()));
            }
        }
    }
    normalized
        .find(query)
        .and_then(|offset| offsets.get(offset).copied())
}

fn search_hit(
    collection: &Collection,
    files: &Files,
    directory: &Path,
    manual: &Manual,
    block_index: usize,
    heading: bool,
    args: &[String],
) -> Result<String> {
    let block = &manual.blocks[block_index];
    let i = block.section;
    let query = args[5]
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    let excerpt_index = if heading {
        manual
            .blocks
            .iter()
            .position(|block| block.section == i && !block.heading)
            .unwrap_or(block_index)
    } else {
        block_index
    };
    let excerpt_block = &manual.blocks[excerpt_index];
    let document = files[&format!("{}.md", manual.name)]
        .lines()
        .collect::<Vec<_>>();
    let body = excerpt_block
        .visible(&document[manual.positions[excerpt_index].clone()].join("\n"))
        .lines()
        .skip(excerpt_block.anchors.len())
        .collect::<Vec<_>>()
        .join("\n");
    let excerpt = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let offset = excerpt.to_lowercase().find(&query).map_or(0, |offset| {
        excerpt.to_lowercase()[..offset]
            .chars()
            .count()
            .saturating_sub(60)
    });
    let mut excerpt = excerpt.chars().skip(offset).take(400).collect::<String>();
    if offset > 0 {
        // Trim only the leading context, never a long word containing the hit.
        let boundary = excerpt
            .char_indices()
            .take(60)
            .find(|(_, c)| c.is_whitespace())
            .map_or(0, |(boundary, _)| boundary);
        excerpt = format!("… {}", excerpt[boundary..].trim_start());
    }
    if body
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .count()
        > offset + 400
        && let Some(boundary) = excerpt.rfind(char::is_whitespace)
    {
        excerpt.truncate(boundary);
        excerpt.push_str(" …");
    }
    let anchor = section_anchor(manual, i);
    let section = format!("{}#{anchor}", manual.name);
    let command = if heading || {
        let mut request = args.to_vec();
        request[4] = section.clone();
        request[6] = "1".into();
        request[7] = "0".into();
        request[8] = "1".into();
        let preview = read(collection, files, directory, &request)?;
        let body = preview[1].split_once("\n\n").unwrap().1;
        let body = body
            .lines()
            .filter(|line| {
                !line.starts_with("Continue: ")
                    && !line.starts_with("Next section: ")
                    && !line.starts_with("Next file: ")
            })
            .collect::<Vec<_>>()
            .join("\n");
        matching_offset(&body, &query).is_some()
    } {
        format!("RShowDoc({section:?})")
    } else {
        let mut line = manual.positions[block_index].start + block.anchors.len() + 1;
        let offset = matching_offset(&body, &query).unwrap();
        let mut column: usize = 1;
        for (_, character) in body.char_indices().take_while(|(index, _)| *index < offset) {
            if character == '\n' {
                line += 1;
                column = 1;
            } else {
                column += 1;
            }
        }
        let column = column.saturating_sub(60).max(1);
        let column = if column > 1 {
            format!(", column = {column}")
        } else {
            String::new()
        };
        format!(".console$manual({:?}, start = {line}{column})", section)
    };
    Ok(format!(
        "{}: {}\n  {}\n  Read: {command}\n",
        manual.name,
        heading_path(manual, i),
        excerpt
    ))
}

fn search(
    collection: &Collection,
    files: &Files,
    directory: &Path,
    args: &[String],
) -> Result<Vec<String>> {
    ensure!(
        format!("{:?}", args[5]).len() <= 2048,
        "Manual search query must fit a 2048-byte continuation command"
    );
    let query = args[5]
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    ensure!(
        !query.is_empty(),
        "Manual search queries must contain non-whitespace text"
    );
    let mut results = Vec::new();
    ensure!(
        args[4].is_empty() || collection.manuals.iter().any(|m| m.name == args[4]),
        "Unknown installed manual: {}",
        args[4]
    );
    for manual in collection
        .manuals
        .iter()
        .filter(|m| args[4].is_empty() || m.name == args[4])
    {
        let document = files[&format!("{}.md", manual.name)]
            .lines()
            .collect::<Vec<_>>();
        for (i, section) in manual.sections.iter().enumerate() {
            let heading = matching_offset(&section.title, &query).is_some();
            if let Some((block_index, _)) =
                manual.blocks.iter().enumerate().find(|(block_index, b)| {
                    b.section == i
                        && (if heading {
                            b.heading
                        } else {
                            !b.heading
                                && matching_offset(
                                    &b.visible(
                                        &document[manual.positions[*block_index].clone()]
                                            .join("\n"),
                                    ),
                                    &query,
                                )
                                .is_some()
                        })
                })
            {
                results.push((!heading, manual, block_index));
            }
        }
    }
    results.sort_by_key(|(body, _, _)| *body);
    let start = args[6].parse::<usize>()? - 1;
    let limit = args[9].parse::<usize>()?;
    let mut output = format!(
        "Manual search: {:?}\nCache: {}\n",
        args[5],
        directory.display()
    );
    if results.is_empty() {
        output.push_str("No matches.\n");
    } else {
        ensure!(
            start < results.len(),
            "Search range begins past the end ({} results)",
            results.len()
        );
        let budget = SEARCH_BUDGET.min(READ_BUDGET.saturating_sub(output.len() + 100));
        let mut body = String::new();
        let mut next = start;
        for (is_body, manual, block_index) in results.iter().skip(start).take(limit) {
            let result = search_hit(
                collection,
                files,
                directory,
                manual,
                *block_index,
                !is_body,
                args,
            )?;
            if body.len() + result.len() + 1 > budget {
                break;
            }
            body.push_str(&result);
            body.push('\n');
            next += 1;
        }
        ensure!(
            next > start,
            "Matched heading path exceeds the search response budget; use the cache files directly"
        );
        output.push_str(&format!(
            "{} matching sections; results {}–{}\n\n{body}",
            results.len(),
            start + 1,
            next
        ));
        if next < results.len() {
            output.push_str(&format!("Continue: .console$manual_search({:?}, manual = {:?}, start = {}, limit = {limit})\n", args[5], args[4], next + 1));
        }
    }
    Ok(vec![directory.to_string_lossy().into(), output])
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_manual(request: SEXP) -> harp::Result<SEXP> {
    let args = Vec::<String>::try_from(&RObject::view(request))?;
    let run = || -> Result<Vec<String>> {
        ensure!(args.len() == 10, "invalid manual request");
        let (directory, prepared, _lock) = materialize(&args)?;
        let collection = &prepared.collection;
        match args[3].as_str() {
            "read" => read(collection, &prepared.files, &directory, &args),
            "search" => search(collection, &prepared.files, &directory, &args),
            _ => bail!("unknown manual operation"),
        }
    };
    let mut response = run().map_err(|error| harp::anyhow!("{error:#}"))?;
    response[1] = response[1].trim_end_matches('\n').into();
    Ok(RObject::from(response).sexp)
}
