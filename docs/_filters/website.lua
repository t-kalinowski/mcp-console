-- Keep the Markdown guides readable on GitHub without website front matter.
function Pandoc(doc)
  local heading = doc.blocks[1]
  if quarto.doc.input_file:match("%.md$") and heading and heading.t == "Header" and heading.level == 1 then
    doc.meta.title = pandoc.MetaInlines(heading.content)
    doc.meta.pagetitle = pandoc.MetaString(pandoc.utils.stringify(heading.content))
    doc.blocks:remove(1)
  end
  return doc
end

-- Link the package README to pkgdown; other files outside the site stay on GitHub.
function Link(link)
  local path, suffix = link.target:match("^([^#?]+)(.*)$")
  if path and not path:match("^[/#]") and not path:match("^%a[%w+.-]*:") then
    local input_dir = pandoc.path.directory(quarto.doc.input_file)
    local target = pandoc.path.normalize(pandoc.path.join({input_dir, path}))
    local root = pandoc.path.directory(quarto.project.directory)
    local source = pandoc.path.make_relative(target, root)
    -- Pandoc normalizes separators but retains parent-directory components.
    while source:match("[^/]+/%.%./") do
      source = source:gsub("[^/]+/%.%./", "")
    end
    if source == "r/README.md" then
      link.target = pandoc.path.make_relative(
        pandoc.path.join({quarto.project.directory, "r/index.html"}), input_dir
      ) .. suffix
    elseif not source:match("^docs/") or source:match("^docs/templates/") then
      link.target = "https://github.com/t-kalinowski/mcp-console/blob/main/" .. source .. suffix
    end
  end
  return link
end
