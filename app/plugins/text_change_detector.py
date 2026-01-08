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



def infer_target_block(user_text, current_svg):
    scores = {}

    for block, value in current_svg.items():
        if not value:
            continue

        overlap = len(
            set(value.lower().split()) &
            set(user_text.lower().split())
        )

        scores[block] = overlap

    return max(scores, key=scores.get) if scores else None

def resolve_partial_text(old_value, user_text):
    """
    Handles:
    - Not X, it’s Y
    - Wrong, should be Y
    - 114 not 113
    """
    lines = [l.strip() for l in user_text.split("\n") if l.strip()]

    for l in lines:
        ll = l.lower()
        if ll.startswith(("it's ", "it is ", "should be ", "correct is ")):
            return l.split(" ", 1)[1].strip()

    return lines[-1]


# ======================================================
# 1️⃣ SVG READER (MERGED FROM svg_text_reader.py)
# ======================================================

# app/plugins/text_change_detector.py

def extract_svg_text_blocks(folder_path: str, caption: str):
    """
    Reads SVG inside a Dropbox folder.
    Robust Logic:
    1. Clean caption (remove .png/.jpg explicitly, lowercase) -> Target Base.
    2. Scan SVGs.
    3. Clean SVG name (remove .svg, split at "---", lowercase).
    4. Match.
    """

    # 1️⃣ Clean the Caption (Remove Extension & Lowercase)
    # Input: "1 - Envelope... .png" -> "1 - envelope..."
    clean_caption = caption.strip()
    lower_caption = clean_caption.lower()

    if lower_caption.endswith(".png"):
        target_base = lower_caption[:-4].strip()
    elif lower_caption.endswith(".jpg"):
        target_base = lower_caption[:-4].strip()
    elif lower_caption.endswith(".jpeg"):
        target_base = lower_caption[:-5].strip()
    else:
        # Fallback if no extension in caption
        target_base = lower_caption

    print(f"🎯 Text Detector searching for base: '{target_base}'")

    dbx = get_system_dropbox_client()
    try:
        entries = dbx.files_list_folder(folder_path).entries
    except Exception as e:
        raise Exception(f"Dropbox Error listing {folder_path}: {e}")

    svg_path = None

    # 2️⃣ Search Strategy 1: Exact Match on Base Name
    for entry in entries:
        if not entry.name.lower().endswith(".svg"):
            continue

        # Clean Dropbox Name: "Name --- Qty.svg" -> "name"
        # Remove extension
        file_name_no_ext = entry.name.rsplit(".", 1)[0]
        # Split at "---", take first part, lowercase, strip spaces
        file_base = file_name_no_ext.split("---")[0].strip().lower()

        if file_base == target_base:
            svg_path = entry.path_display
            print(f"✅ EXACT MATCH FOUND: {entry.name}")
            break

    # 3️⃣ Search Strategy 2: Loose Match (Fallback)
    # If exact match failed (e.g. double space issue), check if target is IN filename
    if not svg_path:
        print("⚠️ Exact match failed, attempting loose match...")
        for entry in entries:
            if not entry.name.lower().endswith(".svg"): continue

            # Check if "1 - envelope..." is inside "1 - envelope... --- 13.svg"
            if target_base in entry.name.lower():
                svg_path = entry.path_display
                print(f"✅ LOOSE MATCH FOUND: {entry.name}")
                break

    if not svg_path:
        # Debugging aid: list what was actually there
        available_files = [e.name for e in entries if e.name.endswith(".svg")]
        raise Exception(
            f"SVG file not found for caption.\n"
            f"Target Base: '{target_base}'\n"
            f"Available SVGs: {available_files}"
        )

    # --- Load SVG ---
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
    # 1️⃣ Keyword guess
    target_block = detect_target_block(customer_text)

    # 2️⃣ SVG-based inference (STRONGER)
    inferred = infer_target_block(customer_text, semantic_svg)
    if inferred:
        target_block = inferred

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

    # 1️⃣ Explicit removals
    if t.startswith(("remove", "delete")):
        return resolve_remove(user_text, semantic_svg)

    # 2️⃣ Corrections FIRST (most important)
    correction = resolve_correction(user_text, semantic_svg)
    if correction:
        return correction

    # 3️⃣ Partial numeric fixes (114 not 113)
    if any(c.isdigit() for c in user_text) and "not" in t:
        return resolve_correction(user_text, semantic_svg)

    # 4️⃣ Full replace ONLY if clearly new content
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
