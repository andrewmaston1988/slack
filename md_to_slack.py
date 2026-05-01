#!/usr/bin/env python3
"""
Convert standard markdown to Slack mrkdwn format.

Code spans and fenced code blocks are protected from conversion and passed
through unchanged — Slack renders backtick syntax natively.

Usage (CLI):
  python md_to_slack.py             # reads from stdin
  python md_to_slack.py "some text" # converts argument

Usage (import):
  from md_to_slack import md_to_slack
"""
import re
import sys


def _parse_table_row(line: str) -> list[str]:
    """Split a markdown table row into stripped cells."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    """True if line is a markdown table separator (|---|---|)."""
    return bool(re.match(r"^\|[\s:|-]+\|$", line.strip()))


def _has_table(text: str) -> bool:
    """Return True if text contains at least one markdown table."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("|") and _is_separator(lines[i + 1]):
            return True
    return False


def _split_segments(text: str) -> list[tuple[str, str]]:
    """
    Split text into alternating ('text', ...) and ('table', ...) segments.
    """
    lines = text.splitlines()
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    while i < len(lines):
        if (lines[i].strip().startswith("|")
                and i + 1 < len(lines)
                and _is_separator(lines[i + 1])):
            # Flush preceding text
            if buf:
                segments.append(("text", "\n".join(buf)))
                buf = []
            # Collect table lines
            table: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i])
                i += 1
            segments.append(("table", "\n".join(table)))
        else:
            buf.append(lines[i])
            i += 1
    if buf:
        segments.append(("text", "\n".join(buf)))
    return segments


_LABEL_STYLE = {"bold": True, "underline": True}
_VALUE_STYLE = {"italic": True}


def _table_to_rich_text_blocks(table_text: str) -> list[dict]:
    """Convert a single markdown table to a list of Slack rich_text blocks."""
    lines = [l for l in table_text.strip().splitlines() if l.strip()]
    if len(lines) < 3:
        return []

    headers = _parse_table_row(lines[0])
    rows = [_parse_table_row(l) for l in lines[2:] if l.strip().startswith("|")]

    first_is_index = bool(re.match(r"^#*\s*(?:no\.?|#|id)?$", headers[0], re.I))

    result: list[dict] = []
    for row_idx, row in enumerate(rows):
        while len(row) < len(headers):
            row.append("")

        elements: list[dict] = []

        if first_is_index:
            idx_val = row[0]
            data_h = headers[1:]
            data_c = row[1:]
            if data_c:
                title = data_c[0]
                elements.append({
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": f"#{idx_val} ", "style": _LABEL_STYLE},
                        {"type": "text", "text": title, "style": _LABEL_STYLE},
                    ],
                })
                field_pairs = list(zip(data_h[1:], data_c[1:]))
            else:
                field_pairs = []
        else:
            data_h = headers
            data_c = row
            field_pairs = list(zip(data_h, data_c))

        for h, c in field_pairs:
            if c and c not in ("—", "-", ""):
                elements.append({
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": f"{h}:", "style": _LABEL_STYLE},
                        {"type": "text", "text": f" {c}", "style": _VALUE_STYLE},
                    ],
                })

        if elements:
            result.append({"type": "rich_text", "elements": elements})
            if row_idx < len(rows) - 1:
                result.append({"type": "divider"})

    return result


def md_to_blocks(text: str) -> list[dict] | None:
    """
    Convert markdown text to Slack Block Kit blocks when tables are present.
    Returns None if no tables detected — caller should fall back to md_to_slack().

    Non-table content becomes section blocks with mrkdwn.
    Table content becomes rich_text blocks with bold/underline labels and italic values.
    """
    if not _has_table(text):
        return None

    blocks: list[dict] = []
    for seg_type, content in _split_segments(text):
        if seg_type == "text":
            converted = md_to_slack(content).strip()
            if not converted:
                continue
            # Section blocks have a 3000-char limit — chunk if needed
            for i in range(0, len(converted), 3000):
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": converted[i : i + 3000]},
                })
        elif seg_type == "table":
            blocks.extend(_table_to_rich_text_blocks(content))

    return blocks or None


def _tables_to_records(text: str) -> str:
    """
    Convert markdown tables to a collapsed record format suited for mobile.

    Each row becomes a small block:
      *#1* *Feature Name*
      *Stage:* 🙋 manual
      *Notes:* some notes here

    The first column is used as an index prefix if it looks like a number or
    '#'. The second column becomes the title on the first line. Remaining
    columns follow as *Header:* value pairs. Cells containing only '—' or
    empty strings are omitted.
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        # Detect table start: a pipe row followed by a separator row
        if (
            lines[i].strip().startswith("|")
            and i + 1 < len(lines)
            and _is_separator(lines[i + 1])
        ):
            headers = _parse_table_row(lines[i])
            i += 2  # skip header + separator

            # Determine if first column is an index (#, No., or numeric values)
            first_is_index = re.match(r"^#*\s*(?:no\.?|#|id)?$", headers[0], re.I) is not None

            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _parse_table_row(lines[i])
                # Pad to header length
                while len(cells) < len(headers):
                    cells.append("")

                if first_is_index:
                    idx_val = cells[0]
                    data_h = headers[1:]
                    data_c = cells[1:]
                    prefix = f"*#{idx_val}*" if idx_val and idx_val != "—" else ""
                else:
                    data_h = headers
                    data_c = cells
                    prefix = ""

                if data_c:
                    # First data column → title line
                    title = data_c[0]
                    line1_parts = [p for p in [prefix, f"*{title}*" if title else ""] if p]
                    out.append(" ".join(line1_parts))
                    # Remaining columns → *Header:* value
                    for h, c in zip(data_h[1:], data_c[1:]):
                        if c and c != "—":
                            out.append(f"  *{h}:* {c}")

                out.append("")  # blank line between records
                i += 1
        else:
            out.append(lines[i])
            i += 1

    return "\n".join(out)


def md_to_slack(text: str) -> str:
    # ------------------------------------------------------------------
    # Step 1 — protect code blocks and inline code from all conversions.
    # Slack renders backtick syntax natively so these pass through as-is.
    # ------------------------------------------------------------------
    placeholders: dict[str, str] = {}
    counter = [0]

    def _protect(m: re.Match) -> str:
        key = f"\x00CODE{counter[0]}\x00"
        placeholders[key] = m.group(0)
        counter[0] += 1
        return key

    text = re.sub(r"```[\s\S]*?```", _protect, text)   # fenced blocks
    text = re.sub(r"`[^`\n]+`", _protect, text)         # inline code

    # ------------------------------------------------------------------
    # Step 2 — conversions
    # ------------------------------------------------------------------

    # Helper: stash Slack bold result so the italic rule can't re-match it
    def _protect_bold(content: str) -> str:
        key = f"\x00BOLD{counter[0]}\x00"
        placeholders[key] = f"*{content}*"
        counter[0] += 1
        return key

    # Headings → bold (strip # prefix)
    text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: _protect_bold(m.group(1)), text, flags=re.MULTILINE)

    # Markdown list items (- or *) → bullet character
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)

    # Unicode → Slack emoji codes
    text = text.replace("✅", ":white_check_mark:")
    text = text.replace("✔", ":white_check_mark:")
    text = text.replace("✓", ":white_check_mark:")
    text = text.replace("✗", ":x:")
    text = text.replace("⚠️", ":warning:")
    text = text.replace("⚠",  ":warning:")
    text = text.replace("🟢", ":large_green_circle:")
    text = text.replace("🟡", ":large_yellow_circle:")
    text = text.replace("🔴", ":red_circle:")
    text = text.replace("🚨", ":rotating_light:")
    text = text.replace("⏳", ":hourglass_flowing_sand:")
    text = text.replace("⚫", ":black_circle:")
    text = text.replace("📋", ":clipboard:")
    text = text.replace("🔀", ":twisted_rightwards_arrows:")
    text = text.replace("🔄", ":arrows_counterclockwise:")
    text = text.replace("🔨", ":hammer:")
    text = text.replace("🔬", ":microscope:")
    text = text.replace("🔸", ":small_orange_diamond:")
    text = text.replace("🔹", ":small_blue_diamond:")
    text = text.replace("🔺", ":small_red_triangle:")
    text = text.replace("🙋", ":raising_hand:")
    text = text.replace("🧪", ":test_tube:")
    text = text.replace("🔥", ":fire:")

    # Typography → ASCII equivalents
    text = text.replace("→", "->")
    text = text.replace("←", "<-")
    text = text.replace("↑", "^")
    text = text.replace("↓", "v")
    text = text.replace("»", ">>")
    text = text.replace("›", ">")
    text = text.replace("–", "-")
    text = text.replace("—", " — ")
    text = text.replace("…", "...")
    text = text.replace("≤", "<=")
    text = text.replace("≥", ">=")
    text = text.replace("≈", "~")
    text = text.replace("×", "x")
    text = text.replace("÷", "/")
    text = text.replace("±", "+/-")

    # Bold italic ***text*** → *_text_*
    text = re.sub(r"\*{3}(.+?)\*{3}", r"*_\1_*", text, flags=re.DOTALL)

    # Bold **text** → *text* (protected so italic rule below won't re-match)
    text = re.sub(r"\*{2}(.+?)\*{2}", lambda m: _protect_bold(m.group(1)), text, flags=re.DOTALL)

    # Italic *text* (single asterisk, not at a bold boundary) → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text, flags=re.DOTALL)

    # Strikethrough ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text, flags=re.DOTALL)

    # Links [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Horizontal rules — remove
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Markdown tables → collapsed record format (mobile-friendly)
    text = _tables_to_records(text)

    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ------------------------------------------------------------------
    # Step 3 — restore protected code regions
    # ------------------------------------------------------------------
    for key, original in placeholders.items():
        text = text.replace(key, original)

    return text.strip()


if __name__ == "__main__":
    import io, os
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if os.path.isfile(arg):
            with open(arg, encoding="utf-8") as f:
                text = f.read()
        else:
            text = arg
        print(md_to_slack(text))
    else:
        text = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig").read()
        print(md_to_slack(text))
