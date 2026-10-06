# Great Tables previews in Python

Great Tables 0.21.0 can produce readable image and Markdown previews through Console's public Python `send` tool.
Both paths need explicit conversion: render an image with `GT.save()` and place it in an open pyplot figure, or convert the table's formatted HTML to Markdown and print it.

A bare `GT` final expression returned its Python object representation in the tested session.
It produced no image and exceeded the text preview budget.
Creating HTML or a PNG file alone does not send a preview to the model.
Use a native MCP integration that preserves image content; the [Python client's text convenience call](PYTHON.md#python-clients) uses image placeholders.

## Tested environment and outcomes

The October 6, 2026 investigation used the checkout executable at `ce674459`, macOS arm64, Python 3.13.15, and these explicitly selected optional packages:

| Component      | Version       |
| -------------- | ------------- |
| Great Tables   | 0.21.0        |
| pandas         | 2.2.3         |
| markdownify    | 1.2.0         |
| Beautiful Soup | 4.15.0        |
| Matplotlib     | 3.10.3        |
| Pillow         | 11.2.1        |
| Selenium       | 4.33.0        |
| Google Chrome  | 154.0.8037.98 |
| ChromeDriver   | 154.0.8037.92 |

This pins one compatible recipe; it does not establish compatibility with other Great Tables releases or renderers.
The packages are optional and are not added to Console's default requirements.

| Public `send` path                 | Direct (`serve --no-sandbox`)                         | Default macOS sandbox                      |
| ---------------------------------- | ----------------------------------------------------- | ------------------------------------------ |
| Formatted HTML to printed Markdown | Passed                                                | Passed                                     |
| `GT.save()` to open pyplot figure  | Passed; returned `image/png`                          | Failed at Selenium's localhost socket bind |
| `GT.save()` without Selenium       | Useful installation diagnostic; session stayed usable | Same result                                |

The direct image response contained one PNG, 520 by 213 pixels and 15,769 bytes.
The investigating model viewed the decoded **MCP response image**, read `Revenue`, `$1,234.50`, and `12.6%`, and saw blue column headers and yellow revenue cells.
The printed Markdown response exposed the same header and values.
The automated image case compares returned PNG bytes with a live same-session pyplot `savefig` reference; it does not use OCR or a platform-specific browser raster snapshot.

Native Linux and Windows runs were not exercised in this investigation.
The optional browser case skips unless explicit browser and driver paths are provided.
A skip does not validate rendering.

## Reproduce the previews

Prepare an isolated Python environment on the host, then select it when launching Console.
Keep `HOME` unchanged and use a temporary `MCP_CONSOLE_HOME` for session configuration.
For the browser-free Markdown example:

```sh
uv venv --python 3.13 /absolute/private-workspace/python
uv pip install --python /absolute/private-workspace/python/bin/python \
    great-tables==0.21.0 pandas==2.2.3 \
    markdownify==1.2.0 beautifulsoup4==4.15.0
mcp-console serve -c python=/absolute/private-workspace/python/bin/python
```

An explicitly selected Python disables managed dependency preparation.
Install additional packages on the host before starting that session; see [Requirements](REQUIREMENTS.md).

Submit the contents of [table.py](../tests/fixtures/great_tables/table.py) as a Python `send` cell.
It constructs two rows with a bold `Quarterly sales` title, `Region`, `Revenue`, and `Share` headers, currency and percentage formatting, and colored headers and revenue cells.
Keep this object assigned to `table`.

### Markdown

Submit [markdown.py](../tests/fixtures/great_tables/markdown.py) in the same session.
It calls `table.as_raw_html()`, parses the rendered table with Beautiful Soup, prints the converted title separately, removes that heading row, and converts the remaining table with markdownify.
Removing the title row keeps the column labels in the Markdown header row.

The public text response is:

```text
**Quarterly sales**

| Region | Revenue | Share |
| --- | --- | --- |
| North | $1,234.50 | 12.6% |
| South | $9,876.00 | 87.4% |
```

This is an explicit HTML-to-Markdown recipe for this small table, not a native Great Tables Markdown export.
It preserves the formatted cell text and bold title.
Markdown loses CSS fills, fonts, widths, and alignment.
The recipe assumes one title row and a rectangular table; it does not promise conversion of spanners, footnotes, embedded images, or arbitrary HTML.
`as_raw_html(inline_css=True)` is unnecessary here and would require the additional `css-inline` package.

### Image

Install the additional rendering packages on the host:

```sh
uv pip install --python /absolute/private-workspace/python/bin/python \
    selenium==4.33.0 pillow==11.2.1 matplotlib==3.10.3
```

Provide an installed Chrome binary and matching ChromeDriver through absolute `MCP_CONSOLE_TEST_CHROME` and `MCP_CONSOLE_TEST_CHROMEDRIVER` environment paths before launching a **direct** session.
Launch that session with:

```sh
mcp-console serve --no-sandbox -c python=/absolute/private-workspace/python/bin/python
```

The image fixture uses these paths explicitly, headless Chrome, and a profile under worker-private `TMPDIR`.
The investigation prepared ChromeDriver with Selenium Manager on the host using a private cache; the evaluated cell does not download a driver.

Submit [image.py](../tests/fixtures/great_tables/image.py) after the table cell.
`GT.save()` renders the styled HTML to a PNG.
Pillow reads it, and Matplotlib places it in an open figure at its pixel dimensions with axes hidden.
At cell end Console captures and closes that figure, returning actual `image/png` MCP content.
The additional `preview-reference.png` file is regression evidence; it is not needed to display the preview.

The browser requires local WebDriver communication.
In the default macOS sandbox, Selenium failed at `socket.bind(("127.0.0.1", 0))` with `PermissionError: [Errno 1] Operation not permitted`, before launching Chrome.
This recipe does not establish sandboxed browser rendering.
No sandbox policy changes were made to obtain the direct result.

Use worker-private `TMPDIR` for the PNG and browser profile; it is writable and deleted when the worker retires.
Copy any artifacts that need to outlive the session before closing it, or use an explicitly granted writable root.
File paths refer to the Console host.
The small example stays below the existing [text and image limits](BUILTIN_RUNTIME.md#output-and-notices); those limits are unchanged.
`GT.show()` was not used: opening a host browser does not establish model-visible content.

## Missing renderer and regression coverage

In a selected environment with Great Tables but no Selenium, this public cell:

```python
table.save("great-table.png")
```

returned a complete Python traceback ending with:

```text
ImportError: Module selenium not found. Run the following to install.

`pip install selenium`
```

No image was returned.
A subsequent Python cell printed `Python still works: 42`.
The test uses a real environment without Selenium and disables managed resolution through explicit Python selection; it does not mock imports.
Browser/driver absence with Selenium installed is not separately covered.

The bounded public suite is [test_great_tables.py](../tests/boundaries/client_server/python/test_great_tables.py):

```sh
scripts/test client_server/python/test_great_tables
```

The Markdown and missing-Selenium cases exercise direct and default sandboxed execution.
To include the direct image case, set the two absolute browser/driver environment paths above.
Tests create disposable virtualenvs with the listed packages and preserve the caller's home and tool environment.
They verify exact Markdown output, a complete useful missing-dependency traceback, continued execution, bounded image dimensions, PNG content identity, and figure closure.
