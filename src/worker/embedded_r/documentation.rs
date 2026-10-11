use harp::object::RObject;
use libr::SEXP;
use std::rc::Rc;

fn heading_level(node: &html2text::Handle) -> Option<u8> {
    let html2text::Element { name, .. } = &node.data else {
        return None;
    };
    match name.local.as_ref() {
        "h1" => Some(1),
        "h2" => Some(2),
        "h3" => Some(3),
        "h4" => Some(4),
        "h5" => Some(5),
        "h6" => Some(6),
        _ => None,
    }
}

fn select_section(dom: &html2text::RcDom, section: &str) -> anyhow::Result<()> {
    let mut pending = vec![dom.document.clone()];
    while let Some(node) = pending.pop() {
        if let html2text::Element { name, attrs, .. } = &node.data
            && attrs.borrow().iter().any(|attr| {
                matches!(attr.name.local.as_ref(), "id" | "name") && attr.value.as_ref() == section
            })
        {
            let selected = if matches!(name.local.as_ref(), "div" | "section") {
                // Texinfo manuals group each section, including its subsections.
                vec![node.clone()]
            } else {
                // Heading IDs and older named anchors delimit sibling blocks.
                let mut start = node.clone();
                while heading_level(&start).is_none() {
                    let parent = start.get_parent().ok_or_else(|| {
                        anyhow::anyhow!("HTML section has no containing block: {section}")
                    })?;
                    if matches!(&parent.data, html2text::Element { name, .. }
                        if matches!(name.local.as_ref(), "body" | "div" | "section"))
                    {
                        break;
                    }
                    start = parent;
                }
                let parent = start.get_parent().ok_or_else(|| {
                    anyhow::anyhow!("HTML section has no containing block: {section}")
                })?;
                let siblings = parent.children.borrow();
                let index = siblings
                    .iter()
                    .position(|node| Rc::ptr_eq(node, &start))
                    .unwrap();
                let mut level = heading_level(&start);
                let mut end = siblings.len();
                for (i, sibling) in siblings.iter().enumerate().skip(index + 1) {
                    if let Some(next_level) = heading_level(sibling) {
                        if level.is_some_and(|level| next_level <= level) {
                            end = i;
                            break;
                        }
                        level.get_or_insert(next_level);
                    }
                }
                siblings[index..end].to_vec()
            };
            // Detach before dropping the remaining DOM: html2text's node
            // destructor drains descendants even when another Rc retains them.
            for child in &selected {
                if let Some(parent) = child.get_parent() {
                    parent
                        .children
                        .borrow_mut()
                        .retain(|node| !Rc::ptr_eq(node, child));
                }
                child.parent.set(Some(Rc::downgrade(&dom.document)));
            }
            *dom.document.children.borrow_mut() = selected;
            return Ok(());
        }
        pending.extend(node.children.borrow().iter().rev().cloned());
    }
    anyhow::bail!("HTML section not found: {section}")
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_documentation_html(
    path: SEXP,
    section: SEXP,
) -> harp::Result<SEXP> {
    let path = String::try_from(RObject::view(path))?;
    let section = String::try_from(RObject::view(section))?;
    let convert = || -> anyhow::Result<String> {
        let html = std::fs::read_to_string(&path)?;
        let config = html2text::config::plain().no_link_wrapping();
        let dom = config.parse_html(html.as_bytes())?;
        if !section.is_empty() {
            select_section(&dom, &section)?;
        }

        // html2text drops table captions. Move them before their tables so the
        // crate renders their text and inline markup alongside the table.
        let mut pending = vec![dom.document.clone()];
        while let Some(node) = pending.pop() {
            let mut children = node.children.borrow_mut();
            let mut lifted = Vec::new();
            for child in children.drain(..) {
                if matches!(&child.data, html2text::Element { name, .. }
                    if name.local.as_ref() == "table")
                {
                    lifted.extend(child.children.borrow_mut().extract_if(.., |caption| {
                        matches!(&caption.data, html2text::Element { name, .. }
                            if name.local.as_ref() == "caption")
                    }));
                }
                lifted.push(child);
            }
            *children = lifted;
            pending.extend(children.iter().cloned());
        }

        let tree = config.dom_to_render_tree(&dom)?;
        Ok(config.render_to_string(tree, 80)?)
    };
    let text = convert().map_err(|error| {
        harp::anyhow!("Unable to convert installed HTML documentation {path}: {error}")
    })?;
    Ok(RObject::from(text).sexp)
}
