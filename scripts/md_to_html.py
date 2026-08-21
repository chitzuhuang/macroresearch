#!/usr/bin/env python3
"""A small, dependency-free Markdown -> HTML converter.

Not a general-purpose Markdown engine — it only supports the constructs the
taiwan-stock-daily-report skill's own reports actually use: ATX headers,
paragraphs, unordered lists, GFM pipe tables, bold/italic, inline code,
links, and horizontal rules. Good enough to render this skill's reports
without pulling in an external dependency inside a restricted sandbox.
"""

from __future__ import annotations

import html
import re

_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    # Placeholders protect already-escaped/rendered spans from being re-matched
    # by later patterns (e.g. a bold marker inside a link label).
    placeholders: list[str] = []

    def stash(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00{len(placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    text = _LINK_RE.sub(
        lambda m: stash(
            f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'
        ),
        text,
    )
    text = _BOLD_RE.sub(lambda m: stash(f"<strong>{m.group(1)}</strong>"), text)
    text = _ITALIC_RE.sub(lambda m: stash(f"<em>{m.group(1)}</em>"), text)

    def unstash(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    # A placeholder's own rendered content can itself contain another
    # placeholder (e.g. bold wrapping inline code) — re.sub does not rescan
    # its own replacement text, so loop until nothing is left to expand.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\x00(\d+)\x00", unstash, text)
    return text


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


def _is_table_divider(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_table(lines: list[str]) -> str:
    header = _split_row(lines[0])
    rows = [_split_row(l) for l in lines[2:]]
    out = ["<table>", "<thead><tr>"]
    for cell in header:
        out.append(f"<th>{_inline(cell)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for cell in row:
            out.append(f"<td>{_inline(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def markdown_to_html(md_text: str) -> str:
    lines = md_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            out.append("<ul>" + "".join(f"<li>{_inline(t)}</li>" for t in list_items) + "</ul>")
            list_items.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            flush_list()
            i += 1
            continue

        header_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if header_match:
            flush_paragraph()
            flush_list()
            level = len(header_match.group(1))
            out.append(f"<h{level}>{_inline(header_match.group(2))}</h{level}>")
            i += 1
            continue

        if re.fullmatch(r"-{3,}", stripped):
            flush_paragraph()
            flush_list()
            out.append("<hr>")
            i += 1
            continue

        if _is_table_row(stripped) and i + 1 < len(lines) and _is_table_divider(lines[i + 1]):
            flush_paragraph()
            flush_list()
            table_lines = [stripped]
            j = i + 1
            while j < len(lines) and _is_table_row(lines[j].strip()):
                table_lines.append(lines[j].strip())
                j += 1
            out.append(_render_table(table_lines))
            i = j
            continue

        list_match = re.match(r"^[-*]\s+(.*)$", stripped)
        if list_match:
            flush_paragraph()
            list_items.append(list_match.group(1))
            i += 1
            continue

        flush_list()
        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    flush_list()
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    text = sys.stdin.read()
    print(markdown_to_html(text))
