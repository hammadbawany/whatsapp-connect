# ======================================================
# TEXT CHANGE DETECTOR
# - Reads SVG from Dropbox
# - Understands semantic meaning of text
# - Detects which text block customer refers to
# - Builds confirmation message
# - DOES NOT edit SVG
# ======================================================

import xml.etree.ElementTree as ET
from io import BytesIO
import copy

from app.plugins.dropbox_plugin import (
    get_system_dropbox_client,
    download_svg_to_memory
)
from app.plugins.design_reply_editor import find_order_folder


SVG_NS = {"svg": "http://www.w3.org/2000/svg"}


# ======================================================
# 1️⃣ SVG READER (MERGED FROM svg_text_reader.py)
# ======================================================

def extract_svg_text_blocks(folder_path: str, caption: str):
    """
    Reads SVG inside a Dropbox folder and extracts structured text blocks.

    Naming rule:
    PNG: <base>.png
    SVG: <base> --- anything.svg
    """

    # --- Find SVG using PNG → SVG rule
    png_base = caption.rsplit(".", 1)[0].strip()
    svg_path = None

    dbx = get_system_dropbox_client()
    entries = dbx.files_list_folder(folder_path).entries

    for entry in entries:
        if not entry.name.lower().endswith(".svg"):
            continue

        svg_name_clean = entry.name.split("---")[0].strip()

        if svg_name_clean == png_base:
            svg_path = entry.path_display
            break

    if not svg_path:
        raise Exception(
            f"SVG file not found for caption. Expected base: '{png_base}'"
        )

    # --- Load SVG
    svg_content = download_svg_to_memory(svg_path)
    tree = ET.parse(svg_content)
    root = tree.getroot()

    # --- Extract text1 (wishes / prefix)
    text1_node = root.find(".//svg:text[@id='text1']", SVG_NS)
    text1_value = text1_node.text.strip() if (
        text1_node is not None and text1_node.text
    ) else None

    # --- Extract text2 (names + extras)
    text2_node = root.find(".//svg:text[@id='text2']", SVG_NS)
    text2_main = None
    text2_extras = []

    if text2_node is not None:
        if text2_node.text and text2_node.text.strip():
            text2_main = text2_node.text.strip()

        for tspan in text2_node.findall("svg:tspan", SVG_NS):
            if tspan.text and tspan.text.strip():
                text2_extras.append(tspan.text.strip())

    return {
        "text1": text1_value,
        "text2_main": text2_main,
        "text2_extras": text2_extras,
        "raw": {
            "text1_node": text1_node,
            "text2_node": text2_node
        }
    }


# ======================================================
# 2️⃣ SEMANTIC NORMALIZER
# ======================================================

def normalize_svg_semantic(svg_data):
    """
    Converts raw SVG extraction into semantic meaning
    """
    return {
        "text1": svg_data.get("text1"),                 # wishes / prefix
        "text2": svg_data.get("text2_main"),            # names
        "extra_information": svg_data.get("text2_extras", [])  # city, designation
    }


# ======================================================
# 3️⃣ TARGET BLOCK DETECTOR
# ======================================================

def detect_target_block(customer_text):
    t = customer_text.lower()

    # Names
    if any(k in t for k in [
        "naam", "name", "pura naam",
        "same line", "aik hi line",
        "mr", "mrs", "&"
    ]):
        return "text2"

    # Extra information
    if any(k in t for k in [
        "city", "shehar", "karachi", "lahore",
        "ceo", "coo", "designation", "party"
    ]):
        return "extra_information"

    # Wishes / prefix
    if any(k in t for k in [
        "from", "wishes", "dua", "duas",
        "best wishes", "with love"
    ]):
        return "text1"

    return None


# ======================================================
# 4️⃣ CONFIRMATION MESSAGE BUILDER
# ======================================================

def build_confirmation_message(semantic_svg):
    """
    Builds confirmation message exactly as final text will appear.
    - Shows only existing text
    - No placeholders
    - No empty lines
    """

    content_lines = []

    # text1 (wishes / prefix)
    text1 = semantic_svg.get("text1")
    if text1:
        content_lines.append(text1.strip())

    # text2 (names)
    text2 = semantic_svg.get("text2")
    if text2:
        content_lines.append(text2.strip())

    # extra information (city, designation, etc.)
    extras = semantic_svg.get("extra_information", [])
    for item in extras:
        if item and item.strip():
            content_lines.append(item.strip())

    # 🚨 Edge case: nothing found
    if not content_lines:
        return (
            "I couldn’t find any text in the design to confirm.\n\n"
            "Please tell me what text you want to add."
        )

    # Build final message
    lines = []
    lines.append("Please confirm the final text 👇\n")
    lines.extend(content_lines)
    lines.append("\nReply with:")
    lines.append("✅ Confirm text")
    lines.append("✏️ Change text")

    return "\n".join(lines)


# ======================================================
# 5️⃣ MAIN ENTRY POINT (USED BY webhook)
# ======================================================

def process_text_change_request(phone, customer_text, reply_caption):
    """
    Phase 1:
    - Detect if message is about TEXT change
    - Read SVG
    - Understand semantic meaning
    - Detect which text block is targeted
    - DO NOT edit anything
    """

    text = customer_text.lower()

    KEYWORDS = [
        "line", "likhen", "likh",
        "add", "remove", "delete",
        "change text", "second line",
        "first line", "neeche", "upar",
        "same line", "one line",
        "font", "bold", "naam", "name"
    ]

    if not any(k in text for k in KEYWORDS):
        return None  # ❌ Not a text-change request

    dbx = get_system_dropbox_client()
    folder_path = find_order_folder(dbx, phone)

    if not folder_path:
        return None

    # --- Read SVG
    svg_data = extract_svg_text_blocks(folder_path, reply_caption)

    # --- Semantic understanding
    semantic_svg = normalize_svg_semantic(svg_data)
    target_block = detect_target_block(customer_text)

    return {
        "folder_path": folder_path,
        "svg_data": svg_data,              # raw (internal)
        "semantic_svg": semantic_svg,      # clean meaning
        "target_block": target_block       # text1 / text2 / extra_information
    }







def looks_like_text_content(text):
    t = text.strip()
    t_lower = t.lower()

    # 1. Ignore very short words (Safety)
    if len(t) < 3:
        return False

    # 2. Ignore common approval words (Critical for Group 5)
    approval_words = ["ok", "done", "yes", "perfect", "good", "nice", "thanks", "confirmed", "confirm"]
    if t_lower in approval_words:
        return False

    # 3. High confidence if it contains digits (Dates/Addresses)
    if any(c.isdigit() for c in t):
        return True

    # 4. Negative Keywords (Commands)
    instruction_verbs = [
        "move", "make", "change", "shift", "align",
        "center", "centre", "increase", "decrease",
        "bold", "italic", "capital", "corner", "shade",
        # Roman Urdu commands
        "upar", "neeche", "side", "kar", "kardo", "thoda"
    ]

    # If the text STARTS with a command verb, it's likely an instruction, not content
    # e.g., "Change to Ali" -> False (Instruction)
    # e.g., "Ali Changezi" -> True (Content)
    first_word = t_lower.split()[0]
    if first_word in instruction_verbs:
        return False

    # 5. Printable Ratio (Filters out emojis/garbage)
    printable_ratio = sum(c.isalnum() or c in "&.,-@/" for c in t) / len(t)

    return printable_ratio > 0.6



def resolve_text_delta(user_text, semantic_svg):
    t = user_text.lower().strip()

    if t.startswith("remove") or t.startswith("delete"):
        return resolve_remove(user_text, semantic_svg)

    if " not " in t:
        return resolve_correction(user_text, semantic_svg)

    if looks_like_text_content(user_text):
        return resolve_full_replace(user_text, semantic_svg)

    return None

def resolve_remove(user_text, semantic_svg):
    target_phrase = (
        user_text.lower()
        .replace("remove", "")
        .replace("delete", "")
        .strip()
    )

    matches = []

    for block, value in semantic_svg.items():
        if not value:
            continue

        if isinstance(value, list):
            for i, v in enumerate(value):
                if target_phrase in v.lower():
                    matches.append(("extra_information", i, target_phrase))
        else:
            if target_phrase in value.lower():
                matches.append((block, None, target_phrase))

    if len(matches) != 1:
        return None

    block, index, phrase = matches[0]

    return {
        "action": "remove_or_replace",
        "target_block": block,
        "index": index,
        "from": phrase,
        "to": ""
    }

def resolve_correction(user_text, semantic_svg):
    parts = user_text.lower().split(" not ")
    if len(parts) != 2:
        return None

    new = parts[0].strip()
    old = parts[1].strip()

    matches = []

    for block, value in semantic_svg.items():
        if not value:
            continue

        if isinstance(value, list):
            for i, v in enumerate(value):
                if old in v.lower():
                    matches.append(("extra_information", i, old, new))
        else:
            if old in value.lower():
                matches.append((block, None, old, new))

    if len(matches) != 1:
        return None

    block, index, old, new = matches[0]

    return {
        "action": "replace_substring",
        "target_block": block,
        "index": index,
        "from": old,
        "to": new
    }


def resolve_full_replace(user_text, semantic_svg):
    if semantic_svg.get("text2"):
        return {
            "action": "replace_block",
            "target_block": "text2",
            "to": user_text.strip()
        }

    if semantic_svg.get("text1"):
        return {
            "action": "replace_block",
            "target_block": "text1",
            "to": user_text.strip()
        }

    return None


def apply_delta(semantic_svg, delta):
    svg = copy.deepcopy(semantic_svg)

    block = delta["target_block"]

    if delta["action"] == "replace_block":
        svg[block] = delta["to"]

    elif delta["action"] == "remove_or_replace":
        if delta["index"] is not None:
            svg[block][delta["index"]] = svg[block][delta["index"]].replace(
                delta["from"], delta["to"]
            )
        else:
            svg[block] = svg[block].replace(delta["from"], delta["to"])

    elif delta["action"] == "replace_substring":
        if delta["index"] is not None:
            svg[block][delta["index"]] = svg[block][delta["index"]].replace(
                delta["from"], delta["to"]
            )
        else:
            svg[block] = svg[block].replace(delta["from"], delta["to"])

    return svg
