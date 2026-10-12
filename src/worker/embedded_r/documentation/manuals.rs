//! Installed HTML -> one logical model -> chunks, TOCs, search, and exports.
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
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

// Bump when conversion or projection changes, including chunking defaults.
const FORMAT: u32 = 7;
const TARGET: usize = 4096;
const BODY_MAX: usize = 5400; // Leave room for hierarchy and navigation.
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
    chunks: Vec<Chunk>,
    anchors: BTreeMap<String, String>,
    toc_anchors: Vec<String>,
}

#[derive(Serialize)]
struct Section {
    title: String,
    level: usize,
    parent: Option<usize>,
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

#[derive(Serialize)]
struct Chunk {
    file: String,
    start: usize,
    end: usize,
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

fn numbered_title(title: &str) -> Option<(&str, &str)> {
    let title = title.strip_prefix("Appendix ").unwrap_or(title);
    let (number, label) = title.split_once(char::is_whitespace)?;
    let number = number.trim_end_matches('.');
    let numbered = number.split('.').all(|n| {
        !n.is_empty()
            && (n.chars().all(|c| c.is_ascii_digit())
                || (n.len() == 1 && n.starts_with(|c: char| c.is_ascii_uppercase())))
    });
    numbered.then_some((number, label.trim()))
}

fn prefix(title: &str, section: usize, front: bool) -> String {
    if front {
        return format!("00.{section:02}");
    }
    if let Some((number, _)) = numbered_title(title) {
        number
            .split('.')
            .filter(|n| !n.is_empty())
            .map(|n| {
                n.parse::<usize>()
                    .map_or_else(|_| n.into(), |n| format!("{n:02}"))
            })
            .collect::<Vec<_>>()
            .join(".")
    } else {
        format!("99.{section:03}")
    }
}

fn slug(title: &str) -> String {
    title
        .to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-")
}

fn chunk(manual: &mut Manual) {
    let mut start = 0;
    let mut size = 0;
    let mut ranges = Vec::new();
    for (i, block) in manual.blocks.iter().enumerate() {
        let bytes =
            block.text.len() + block.anchors.iter().map(|a| a.len() + 15).sum::<usize>() + 2;
        let chapter = block.heading && manual.sections[block.section].level <= 2;
        if i > start && (size + bytes > BODY_MAX || size >= TARGET || chapter) {
            // Move a heading with its first body block, including an oversized
            // code example/table. Never leave a heading stranded in a prior file.
            let cut = if manual.blocks[i - 1].heading {
                i - 1
            } else {
                i
            };
            if cut > start {
                ranges.push((start, cut));
                start = cut;
                size = manual.blocks[cut..i]
                    .iter()
                    .map(|b| {
                        b.text.len() + b.anchors.iter().map(|a| a.len() + 15).sum::<usize>() + 2
                    })
                    .sum();
            }
        }
        size += bytes;
    }
    ranges.push((start, manual.blocks.len()));
    let mut counts = BTreeMap::new();
    for (start, end) in ranges {
        let section = manual.blocks[start].section;
        let title = &manual.sections[section].title;
        let count = counts.entry(section).or_insert(0);
        *count += 1;
        let numbered = numbered_title(title).filter(|_| section > 0);
        let label = numbered.map_or(title.as_str(), |(_, label)| label);
        let front = section == 0
            || (numbered.is_none()
                && !manual.sections[..section]
                    .iter()
                    .any(|s| numbered_title(&s.title).is_some()));
        let file = format!(
            "{}-{}-part-{:03}.md",
            prefix(title, section, front),
            slug(label),
            count
        );
        for block in &manual.blocks[start..end] {
            for anchor in block.all_anchors() {
                manual.anchors.insert(anchor.clone(), file.clone());
            }
        }
        manual.chunks.push(Chunk { file, start, end });
    }
    for anchor in &manual.toc_anchors {
        manual.anchors.insert(anchor.clone(), "index.md".into());
    }
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
    chunk(&mut manual);
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

fn location(collection: &Collection, current: &Manual, url: &str, complete: bool) -> String {
    if url.contains(':') || url.starts_with("//") {
        return url.into();
    }
    let (file, anchor) = url.split_once('#').unwrap_or((url, ""));
    let name = if file.is_empty() {
        current.name.as_str()
    } else {
        file.strip_suffix(".html").unwrap_or(file)
    };
    if let Some(manual) = collection.manuals.iter().find(|m| m.name == name) {
        let target = if anchor.is_empty() {
            Some("index.md")
        } else {
            manual.anchors.get(anchor).map(String::as_str)
        };
        if let Some(target) = target {
            if complete && name == current.name && !anchor.is_empty() {
                return format!("#{anchor}");
            }
            let path = if name == current.name && !complete {
                target.into()
            } else {
                format!("../{name}/{target}")
            };
            return format!("{path}#{anchor}");
        }
    }
    // A visible diagnostic is preferable to a link to an invented cache file.
    format!("unavailable local target: {url}")
}

fn project(collection: &Collection, manual: &Manual, block: &Block, complete: bool) -> String {
    let mut text = block.text.clone();
    for (i, link) in block.links.iter().enumerate() {
        let destination = location(collection, manual, link, complete);
        text = text.replace(&format!("\u{1f}{i}\u{1f}"), &destination);
    }
    // Unavailable local destinations remain readable, without a broken link.
    text = text.replace(
        "](unavailable local target: ",
        "] [unavailable local target: ",
    );
    for link in block.links.iter().filter(|link| {
        location(collection, manual, link, complete).starts_with("unavailable local target: ")
    }) {
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

fn section_location(manual: &Manual, section: usize) -> (&str, &str) {
    let block = manual
        .blocks
        .iter()
        .position(|b| b.section == section)
        .unwrap();
    let file = &manual
        .chunks
        .iter()
        .find(|c| c.start <= block && block < c.end)
        .unwrap()
        .file;
    (
        file,
        manual.blocks[block]
            .anchors
            .first()
            .map_or("", String::as_str),
    )
}

fn toc(manual: &Manual, compact: bool) -> String {
    let mut text = format!("# {}\n\n[Collection](../index.md)\n\n", manual.title);
    if !compact {
        for anchor in &manual.toc_anchors {
            text.push_str(&format!("{}\n", anchor_html(anchor)));
        }
    }
    text.push('\n');
    for (i, section) in manual.sections.iter().enumerate() {
        if compact && section.level > 2 {
            continue;
        }
        let (file, anchor) = section_location(manual, i);
        text.push_str(&format!(
            "{}- [{}]({file}#{anchor})\n",
            "  ".repeat(section.level.saturating_sub(1)),
            section.title
        ));
    }
    text.push_str(&format!("\nRead: `RShowDoc(\"{}#original-anchor\")`\nSearch: `.console$manual_search(\"phrase\", manual = \"{}\")`\n", manual.name, manual.name));
    if compact {
        text.push_str(&format!(
            "Full TOC: `.console$manual(\"{}/index.md\")`\n",
            manual.name
        ));
    }
    text
}

fn content(collection: &Collection, manual: &Manual, chunk: &Chunk, index: usize) -> String {
    let section = manual.blocks[chunk.start].section;
    let parent = manual.sections[section]
        .parent
        .map(|p| {
            let (file, anchor) = section_location(manual, p);
            format!("[Parent]({file}#{anchor})")
        })
        .unwrap_or_else(|| "[Parent](index.md)".into());
    let mut text = format!(
        "Manual: {} ({})\nLocation: {}\n\n{parent} · [TOC](index.md)",
        manual.title,
        manual.name,
        heading_path(manual, section)
    );
    if index > 0 {
        text.push_str(&format!(" · [Previous]({})", manual.chunks[index - 1].file));
    }
    if let Some(next) = manual.chunks.get(index + 1) {
        text.push_str(&format!(" · [Next]({})", next.file));
    }
    text.push_str("\n\n");
    for block in &manual.blocks[chunk.start..chunk.end] {
        text.push_str(&project(collection, manual, block, false));
    }
    text
}

fn fingerprint(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn block_positions(
    collection: &Collection,
    manual: &Manual,
    chunk: &Chunk,
    index: usize,
) -> Vec<usize> {
    let prefix = Chunk {
        file: chunk.file.clone(),
        start: chunk.start,
        end: chunk.start,
    };
    let mut line = content(collection, manual, &prefix, index)
        .bytes()
        .filter(|c| *c == b'\n')
        .count();
    let mut positions = Vec::new();
    for block in &manual.blocks[chunk.start..chunk.end] {
        positions.push(line);
        line += project(collection, manual, block, false)
            .bytes()
            .filter(|c| *c == b'\n')
            .count();
    }
    positions
}

fn anchor_positions(
    collection: &Collection,
    manual: &Manual,
    chunk: &Chunk,
    lines: &[usize],
) -> BTreeMap<String, usize> {
    let mut positions = BTreeMap::new();
    for (block, line) in manual.blocks[chunk.start..chunk.end].iter().zip(lines) {
        let projected = project(collection, manual, block, false);
        for anchor in block.all_anchors() {
            let offset = projected
                .lines()
                .position(|text| text.contains(&anchor_html(anchor)))
                .unwrap();
            positions.insert(anchor.clone(), line + offset);
        }
    }
    positions
}

fn projected_files(collection: &Collection) -> Result<Files> {
    let mut files = Files::new();
    let mut index = format!("# Installed R manuals\n\n{}\n\n", collection.r_version);
    for manual in &collection.manuals {
        index.push_str(&format!(
            "- [{} ({})]({}/index.md)\n",
            manual.title, manual.name, manual.name
        ));
        files.insert(format!("{}/index.md", manual.name), toc(manual, false));
        for (i, chunk) in manual.chunks.iter().enumerate() {
            files.insert(
                format!("{}/{}", manual.name, chunk.file),
                content(collection, manual, chunk, i),
            );
        }
    }
    index.push_str("\nRead: `RShowDoc(\"R-exts\")`\nSearch: `.console$manual_search(\"phrase\")`\nExport: `.console$manual_export(\"R-exts\")`\n\nThe per-manual directories contain non-overlapping content files and TOCs.\nComplete exports live separately in `exports/`; exclude them from filesystem searches.\n");
    files.insert("index.md".into(), index);
    files.insert(
        "manifest.json".into(),
        serde_json::to_string_pretty(collection)?,
    );
    Ok(files)
}

fn matches_file(path: &Path, expected: &str) -> std::io::Result<bool> {
    let file = fs::File::open(path)?;
    if file.metadata()?.len() != expected.len() as u64 {
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
    let mut pending = vec![PathBuf::new()];
    while let Some(parent) = pending.pop() {
        for entry in fs::read_dir(directory.join(&parent))? {
            let entry = entry?;
            let relative = parent.join(entry.file_name());
            let kind = entry.file_type()?;
            if kind.is_dir() {
                if relative != Path::new("exports")
                    && !prepared
                        .collection
                        .manuals
                        .iter()
                        .any(|manual| relative == Path::new(&manual.name))
                {
                    return Ok(false);
                }
                pending.push(relative);
            } else if kind.is_file() {
                if let Some(text) = expected.remove(&relative) {
                    if !matches_file(&entry.path(), text)? {
                        return Ok(false);
                    }
                } else if let Some(manual) = prepared.collection.manuals.iter().find(|manual| {
                    relative == Path::new("exports").join(format!("{}.md", manual.name))
                }) {
                    if !matches_file(&entry.path(), &export_text(&prepared.collection, manual))? {
                        return Ok(false);
                    }
                } else {
                    return Ok(false);
                }
            } else {
                // Do not follow links or read special files from a writable cache.
                return Ok(false);
            }
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
            let files = Arc::new(projected_files(&collection)?);
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
    let mut anchor = "";
    let relative = if topic.is_empty() {
        "index.md".into()
    } else if let Some((name, fragment)) = topic.split_once('#') {
        anchor = fragment;
        let manual = collection
            .manuals
            .iter()
            .find(|m| m.name == name)
            .context("Unknown installed manual")?;
        let file = manual
            .anchors
            .get(fragment)
            .with_context(|| format!("Unknown manual anchor: {topic}"))?;
        format!("{name}/{file}")
    } else if collection.manuals.iter().any(|m| m.name == *topic) {
        format!("{topic}/index.md")
    } else if collection.manuals.iter().any(|m| {
        m.chunks
            .iter()
            .any(|c| format!("{}/{}", m.name, c.file) == *topic)
            || format!("{}/index.md", m.name) == *topic
    }) {
        topic.clone()
    } else {
        bail!("Unknown installed manual or content file: {topic}")
    };
    let path = directory.join(&relative);
    let mut text = if let Some(manual) = collection.manuals.iter().find(|m| m.name == *topic) {
        toc(manual, true)
    } else {
        // Reading uses the source-derived projection even if another worker
        // changes a cache file after the integrity check.
        files
            .get(&relative)
            .context("Unknown manual content file")?
            .clone()
    };
    let mut physical_lines = (1..=text.lines().count())
        .map(|line| (relative.clone(), line))
        .collect::<Vec<_>>();
    let mut positions = BTreeMap::new();
    let mut section_lines = Vec::new();
    if !collection.manuals.iter().any(|m| m.name == *topic)
        && let Some(manual) = collection
            .manuals
            .iter()
            .find(|m| relative.starts_with(&format!("{}/", m.name)))
    {
        if relative.ends_with("/index.md") {
            for anchor in &manual.toc_anchors {
                positions.insert(
                    anchor.clone(),
                    text.lines()
                        .position(|line| line == anchor_html(anchor))
                        .context("Cached TOC anchor is missing")?,
                );
            }
        } else {
            let (index, chunk) = manual
                .chunks
                .iter()
                .enumerate()
                .find(|(_, c)| relative == format!("{}/{}", manual.name, c.file))
                .unwrap();
            let block_lines = block_positions(collection, manual, chunk, index);
            positions = anchor_positions(collection, manual, chunk, &block_lines);
            section_lines = block_lines
                .into_iter()
                .zip(&manual.blocks[chunk.start..chunk.end])
                .map(|(line, block)| (line, block.section))
                .collect();
        }
    }
    // Physical cache files are storage boundaries, not reading boundaries.
    // Extend an initial anchor view to the next logical heading, recording
    // original file/line coordinates for exact physical continuations.
    if !anchor.is_empty() && args[6] == "1" && args[8] == "1" && !relative.ends_with("/index.md") {
        let manual = collection
            .manuals
            .iter()
            .find(|m| relative.starts_with(&format!("{}/", m.name)))
            .unwrap();
        let current = manual
            .blocks
            .iter()
            .position(|block| block.all_anchors().any(|id| id == anchor))
            .unwrap();
        let end = manual.blocks[current + 1..]
            .iter()
            .position(|block| block.heading)
            .map_or(manual.blocks.len(), |offset| current + offset + 2);
        let first = manual
            .chunks
            .iter()
            .position(|chunk| relative.ends_with(&chunk.file))
            .unwrap();
        for (index, chunk) in manual
            .chunks
            .iter()
            .enumerate()
            .skip(first + 1)
            .take_while(|(_, chunk)| chunk.start < end)
        {
            let source_lines = block_positions(collection, manual, chunk, index);
            let file = format!("{}/{}", manual.name, chunk.file);
            for (block, source) in manual.blocks[chunk.start..chunk.end.min(end)]
                .iter()
                .zip(source_lines)
            {
                let projected = project(collection, manual, block, false);
                let line = physical_lines.len();
                section_lines.push((line, block.section));
                for id in block.all_anchors() {
                    let offset = projected
                        .lines()
                        .position(|text| text.contains(&anchor_html(id)))
                        .unwrap();
                    positions.insert(id.clone(), line + offset);
                }
                physical_lines.extend(
                    (source + 1..=source + projected.lines().count())
                        .map(|line| (file.clone(), line)),
                );
                text.push_str(&projected);
            }
        }
    }
    let lines = text.lines().collect::<Vec<_>>();
    let markers = positions.values().copied().collect::<BTreeSet<_>>();
    let mut start = args[6].parse::<usize>()? - 1;
    let column = args[8].parse::<usize>()? - 1;
    if !anchor.is_empty() && start == 0 && column == 0 {
        start = *positions
            .get(anchor)
            .context("Cached manual anchor is missing")?;
    }
    ensure!(
        start < lines.len(),
        "Line range begins past the end ({} lines)",
        lines.len()
    );
    let requested = args[7].parse::<usize>()?;
    let mut end = if requested == 0 {
        lines.len()
    } else {
        (start + requested).min(lines.len())
    };
    let mut following = None;
    if !anchor.is_empty() && args[6] == "1" && column == 0 && !relative.ends_with("/index.md") {
        let manual = collection
            .manuals
            .iter()
            .find(|m| relative.starts_with(&format!("{}/", m.name)))
            .unwrap();
        let current = manual
            .blocks
            .iter()
            .position(|b| b.all_anchors().any(|a| a == anchor))
            .unwrap();
        if let Some(block) = manual.blocks[current + 1..].iter().find(|b| b.heading)
            && let Some(line) = block
                .anchors
                .first()
                .and_then(|a| positions.get(a))
                .copied()
        {
            end = end.min(line);
            following = Some((
                line,
                format!(
                    "{}#{}",
                    manual.name,
                    block.anchors.first().map_or("", String::as_str)
                ),
            ));
        }
    }
    let mut output = format!("Cache: {}\nFile: {relative}\n", directory.display());
    let mut file_metadata_end = output.len();
    let mut displayed_files = BTreeSet::from([relative.clone()]);
    if !anchor.is_empty() || start > 0 && !section_lines.is_empty() {
        let manual = collection
            .manuals
            .iter()
            .find(|m| relative.starts_with(&format!("{}/", m.name)))
            .unwrap();
        let section = if let Some(block) = manual
            .blocks
            .iter()
            .find(|b| !anchor.is_empty() && b.all_anchors().any(|a| a == anchor))
        {
            Some(block.section)
        } else {
            section_lines
                .iter()
                .rev()
                .find(|(line, _)| *line <= start)
                .map(|(_, section)| *section)
        };
        if let Some(section) = section {
            output.push_str(&format!("Location: {}\n", heading_path(manual, section)));
        } else {
            output.push_str(&format!("Location: {} > Table of Contents\n", manual.title));
        }
    }
    output.push('\n');
    let budget = output.len() + READ_BODY_BUDGET.min(READ_BUDGET.saturating_sub(output.len()));
    let mut next = start;
    let mut next_column = column;
    for (i, line) in lines.iter().enumerate().take(end).skip(start) {
        let mut visible = (*line).to_string();
        if markers.contains(&i) {
            for (anchor, position) in &positions {
                if *position == i {
                    visible = visible.replace(&anchor_html(anchor), "");
                }
            }
            if visible.trim().is_empty() {
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
        let file = &physical_lines[i].0;
        let metadata = if displayed_files.contains(file) {
            String::new()
        } else {
            format!("Also read: {file}\n")
        };
        let available = budget.saturating_sub(output.len() + metadata.len());
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
        if !metadata.is_empty() {
            output.insert_str(file_metadata_end, &metadata);
            file_metadata_end += metadata.len();
            displayed_files.insert(file.clone());
        }
        output.push_str(&part);
        output.push('\n');
        next = i + 1;
        next_column = 0;
    }
    if let Some((boundary, following)) = following
        && next == boundary
    {
        output.push_str(&format!("\nNext section: RShowDoc({following:?})\n"));
    } else if next < lines.len() {
        output.push_str(&format!(
            "\nContinue: .console$manual({:?}, start = {}, column = {})\n",
            if topic.is_empty() {
                ""
            } else {
                &physical_lines[next].0
            },
            physical_lines[next].1,
            next_column + 1
        ));
    } else if let Some(manual) = collection
        .manuals
        .iter()
        .find(|m| relative.starts_with(&format!("{}/", m.name)))
        && let Some(index) = manual
            .chunks
            .iter()
            .position(|c| relative.ends_with(&c.file))
        && let Some(chunk) = manual.chunks.get(index + 1)
    {
        output.push_str(&format!(
            "\nNext file: .console$manual({:?})\n",
            format!("{}/{}", manual.name, chunk.file)
        ));
    }
    Ok(vec![path.to_string_lossy().into(), output])
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
    let query = args[5].to_lowercase();
    let chunk = manual
        .chunks
        .iter()
        .find(|c| c.start <= block_index && block_index < c.end)
        .unwrap();
    let excerpt_block = if heading {
        manual
            .blocks
            .iter()
            .find(|b| b.section == i && !b.heading)
            .unwrap_or(block)
    } else {
        block
    };
    let projected = project(collection, manual, excerpt_block, false);
    let body = excerpt_block
        .visible(&projected)
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
    let (_, anchor) = section_location(manual, i);
    let section = format!("{}#{anchor}", manual.name);
    let command = if heading || {
        let mut request = args.to_vec();
        request[4] = section.clone();
        request[6] = "1".into();
        request[7] = "0".into();
        request[8] = "1".into();
        let preview = read(collection, files, directory, &request)?;
        let body = preview[1].split_once("\n\n").unwrap().1;
        body.lines()
            .filter(|line| {
                !line.starts_with("Continue: ")
                    && !line.starts_with("Next section: ")
                    && !line.starts_with("Next file: ")
            })
            .collect::<Vec<_>>()
            .join("\n")
            .to_lowercase()
            .contains(&query)
    } {
        format!("RShowDoc({section:?})")
    } else {
        let prefix = Chunk {
            file: chunk.file.clone(),
            start: chunk.start,
            end: block_index,
        };
        let prefix = content(
            collection,
            manual,
            &prefix,
            manual
                .chunks
                .iter()
                .position(|c| c.file == chunk.file)
                .unwrap(),
        );
        let mut line = prefix.bytes().filter(|c| *c == b'\n').count() + block.anchors.len() + 1;
        let offset = body.to_lowercase().find(&query).unwrap_or(0);
        let mut consumed = 0;
        let mut column: usize = 1;
        for character in body.chars() {
            let bytes = character.to_lowercase().map(char::len_utf8).sum::<usize>();
            if consumed + bytes > offset {
                break;
            }
            consumed += bytes;
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
        format!(
            ".console$manual({:?}, start = {line}{column})",
            format!("{}/{}", manual.name, chunk.file)
        )
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
    let query = args[5].to_lowercase();
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
        for (i, section) in manual.sections.iter().enumerate() {
            let heading = section.title.to_lowercase().contains(&query);
            if let Some((block_index, _)) = manual.blocks.iter().enumerate().find(|(_, b)| {
                b.section == i
                    && (if heading {
                        b.heading
                    } else {
                        !b.heading && b.visible(&b.text).to_lowercase().contains(&query)
                    })
            }) {
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
        let budget = TARGET.min(READ_BUDGET.saturating_sub(output.len() + 100));
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

fn export_text(collection: &Collection, manual: &Manual) -> String {
    let mut text = format!(
        "<!-- Installed {}, {}. Converted under the permission notice below. -->\n\n",
        manual.name, collection.r_version
    );
    for anchor in &manual.toc_anchors {
        text.push_str(&format!("{}\n", anchor_html(anchor)));
    }
    for block in &manual.blocks {
        text.push_str(&project(collection, manual, block, true));
    }
    text
}

fn export(collection: &Collection, directory: &Path, name: &str) -> Result<Vec<String>> {
    let manual = collection
        .manuals
        .iter()
        .find(|m| m.name == name)
        .context("Unknown installed manual")?;
    let exports = directory.join("exports");
    fs::create_dir_all(&exports)?;
    let path = exports.join(format!("{name}.md"));
    let text = export_text(collection, manual);
    if !matches_file(&path, &text).unwrap_or(false) {
        // Share the reclaimable identity-scoped staging directory. The caller
        // holds the publication lock until this complete export is renamed.
        let staging = directory.with_file_name(format!(
            ".{}.incomplete",
            directory.file_name().unwrap().to_string_lossy()
        ));
        fs::create_dir(&staging)?;
        let temporary = staging.join("export.md");
        fs::write(&temporary, text)?;
        fs::rename(&temporary, &path)?;
        fs::remove_dir(&staging)?;
    }
    Ok(vec![
        path.to_string_lossy().into(),
        format!(
            "Complete Markdown: {}\nRead bounded ranges with readLines(); exports are excluded from manual search.\n",
            path.display()
        ),
    ])
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
            "export" => export(collection, &directory, &args[4]),
            _ => bail!("unknown manual operation"),
        }
    };
    let mut response = run().map_err(|error| harp::anyhow!("{error:#}"))?;
    response[1] = response[1].trim_end_matches('\n').into();
    Ok(RObject::from(response).sexp)
}
