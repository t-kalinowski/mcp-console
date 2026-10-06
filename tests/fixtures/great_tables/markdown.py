from bs4 import BeautifulSoup
from markdownify import markdownify

# Convert the formatted HTML, keeping the title outside the Markdown table.
rendered = BeautifulSoup(table.as_raw_html(), "html.parser").select_one("table")
heading = rendered.select_one("tr.gt_heading")
print(markdownify(heading.select_one("td").decode_contents()).strip())
print()
heading.decompose()
print(markdownify(str(rendered)).strip())
