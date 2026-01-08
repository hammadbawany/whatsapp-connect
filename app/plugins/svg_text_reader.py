# app/plugins/svg_text_reader.py

import re
import xml.etree.ElementTree as ET
from io import BytesIO

from app.plugins.dropbox_plugin import download_svg_to_memory


SVG_NS = {"svg": "http://www.w3.org/2000/svg"}


def extract_svg_text_blocks(folder_path: str, caption: str):
    """
    Reads SVG inside a Dropbox folder and extracts structured text blocks.

    Returns:
        {
          text1: str | None,
          text2_main: str | None,
          text2_extras: list[str],
          raw: {
              text1_node: Element | None,
              text2_node: Element | None
          }
        }
    """

    # --------------------------------------------------
    # 1️⃣ Locate SVG file using PNG → SVG naming rule
    # --------------------------------------------------
    png_base = caption.rsplit(".", 1)[0].strip()
    svg_path = None

    from app.plugins.dropbox_plugin import get_system_dropbox_client
    dbx = get_system_dropbox_client()

    entries = dbx.files_list_folder(folder_path).entries

    for entry in entries:
        if not entry.name.lower().endswith(".svg"):
            continue

        # Remove ' --- anything' from SVG filename
        svg_name_clean = entry.name.split("---")[0].strip()

        # Match base name
        if svg_name_clean == png_base:
            svg_path = entry.path_display
            break

    if not svg_path:
        raise Exception(
            f"SVG file not found for caption. Expected base: '{png_base}'"
        )

    # --------------------------------------------------
    # 2️⃣ Load SVG into memory
    # --------------------------------------------------
    svg_content = download_svg_to_memory(svg_path)
    if not svg_content:
        raise Exception("Failed to download SVG")

    tree = ET.parse(svg_content)
    root = tree.getroot()

    # --------------------------------------------------
    # 3️⃣ Extract text1
    # --------------------------------------------------
    text1_node = root.find(".//svg:text[@id='text1']", SVG_NS)
    text1_value = None

    if text1_node is not None and text1_node.text:
        text1_value = text1_node.text.strip()

    # --------------------------------------------------
    # 4️⃣ Extract text2 (main + tspans)
    # --------------------------------------------------
    text2_node = root.find(".//svg:text[@id='text2']", SVG_NS)
    text2_main = None
    text2_extras = []

    if text2_node is not None:
        # Main text (excluding tspans)
        if text2_node.text:
            text2_main = text2_node.text.strip()

        # Extra lines (tspans)
        for tspan in text2_node.findall("svg:tspan", SVG_NS):
            if tspan.text and tspan.text.strip():
                text2_extras.append(tspan.text.strip())

    # --------------------------------------------------
    # 5️⃣ Normalize empty strings
    # --------------------------------------------------
    if text1_value == "":
        text1_value = None

    if text2_main == "":
        text2_main = None

    # --------------------------------------------------
    # 6️⃣ Final structured output
    # --------------------------------------------------
    return {
        "text1": text1_value,
        "text2_main": text2_main,
        "text2_extras": text2_extras,
        "raw": {
            "text1_node": text1_node,
            "text2_node": text2_node
        }
    }
