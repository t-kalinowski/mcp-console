import os
from pathlib import Path

import matplotlib.pyplot as plt
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

options = webdriver.ChromeOptions()
options.binary_location = os.environ["MCP_CONSOLE_TEST_CHROME"]
options.add_argument("--headless=new")
options.add_argument(
    "--user-data-dir=" + str(Path(os.environ["TMPDIR"]) / "chrome-profile")
)
artifact = Path(os.environ["TMPDIR"]) / "great-table.png"
with webdriver.Chrome(
    service=Service(os.environ["MCP_CONSOLE_TEST_CHROMEDRIVER"]), options=options
) as browser:
    table.save(artifact, web_driver=browser, window_size=(900, 700))

# Console captures open pyplot figures at cell end.
with Image.open(artifact) as image:
    width, height = image.size
    assert 100 <= width <= 1000 and 100 <= height <= 1000, image.size
    assert any(lo < hi for lo, hi in image.convert("RGB").getextrema())
    pixels = image.copy()
figure = plt.figure(figsize=(width / 100, height / 100), dpi=100)
axes = figure.add_axes((0, 0, 1, 1))
axes.imshow(pixels)
axes.axis("off")
figure.savefig(Path(os.environ["TMPDIR"]) / "preview-reference.png", format="png")
print("Great Tables image preview")
