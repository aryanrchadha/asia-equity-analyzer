"""Render generated markdown reports as self-contained, shareable HTML pages.

This is a deliberately narrow markdown renderer covering exactly the subset the
reports use: headings, tables, lists, blockquotes, rules, and inline emphasis /
code / links.

It is hand-rolled rather than delegated to a markdown library for one reason:
report bodies are model-generated text derived from untrusted filings, and the
common libraries pass raw HTML through untouched. Here every character is
HTML-escaped *before* any formatting is applied, so a filing containing a
<script> tag renders as visible text on the page instead of executing in
whoever's browser the report gets shared with.
"""

from __future__ import annotations

import html
import os
import re

# Only these URL schemes become clickable links; anything else (javascript:,
# data:, vbscript:) renders as plain text.
SAFE_URL = re.compile(r"^(?:https?://|mailto:|#|/|\./)", re.IGNORECASE)

_CODE_TOKEN = "\x00code{}\x00"
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# Split on pipes that aren't backslash-escaped (report_writer escapes literal
# pipes in filenames as \| so they don't split a row into extra cells).
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

CSS = """
:root {
  --paper: #faf9f7;
  --ink: #1c1b19;
  --muted: #6b6862;
  --rule: #e0ddd6;
  --accent: #1f5f5b;
  --surface: #ffffff;
  --zebra: #f4f2ee;
  --shadow: rgba(28, 27, 25, 0.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #17191a;
    --ink: #e6e4e0;
    --muted: #9a978f;
    --rule: #2e3133;
    --accent: #6fb3ac;
    --surface: #1d2022;
    --zebra: #212527;
    --shadow: rgba(0, 0, 0, 0.3);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 3rem 1.5rem 6rem;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
main { max-width: 54rem; margin: 0 auto; }
h1, h2, h3, h4 {
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-weight: 600;
  line-height: 1.25;
  margin: 2.5rem 0 0.75rem;
  letter-spacing: -0.01em;
}
h1 { font-size: 2rem; margin-top: 0; }
h2 {
  font-size: 1.4rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--rule);
}
h3 { font-size: 1.1rem; }
h4 { font-size: 1rem; color: var(--muted); }
p { margin: 0 0 1rem; }
ul, ol { margin: 0 0 1rem; padding-left: 1.4rem; }
li { margin: 0.3rem 0; }
li > ul, li > ol { margin: 0.3rem 0; }
a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
strong { font-weight: 650; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.5rem 0; }
code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.86em;
  background: var(--zebra);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 0.1em 0.35em;
  word-break: break-word;
}
blockquote {
  margin: 0 0 1.5rem;
  padding: 0.85rem 1.1rem;
  background: var(--surface);
  border-left: 3px solid var(--accent);
  border-radius: 0 3px 3px 0;
  color: var(--muted);
  font-size: 0.92rem;
}
blockquote p:last-child { margin-bottom: 0; }
.table-wrap {
  overflow-x: auto;
  margin: 0 0 1.5rem;
  border: 1px solid var(--rule);
  border-radius: 4px;
  background: var(--surface);
}
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td {
  padding: 0.55rem 0.8rem;
  text-align: left;
  border-bottom: 1px solid var(--rule);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
th {
  background: var(--zebra);
  font-weight: 600;
  font-size: 0.82rem;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--muted);
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr:nth-child(even) { background: var(--zebra); }
td.num, th.num { text-align: right; }
td.center, th.center { text-align: center; }
.masthead { margin-bottom: 2.5rem; }
.masthead .eyebrow {
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 600;
  margin: 0 0 0.5rem;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 0.5rem;
  margin: 1rem 0 0;
  padding: 0;
  list-style: none;
}
.meta li {
  font-size: 0.76rem;
  color: var(--muted);
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 999px;
  padding: 0.2rem 0.7rem;
  margin: 0;
  white-space: nowrap;
}
.meta b { color: var(--ink); font-weight: 600; }
@media print {
  body { padding: 0; background: #fff; color: #000; }
  .table-wrap { break-inside: avoid; }
  h2, h3 { break-after: avoid; }
}
"""

# Front-matter keys surfaced as chips, in display order, with their labels.
META_FIELDS = (
    ("analysis_date", "Generated"),
    ("model", "Model"),
    ("period_count", "Periods"),
    ("company_count", "Companies"),
    ("agent_count", "Agents"),
    ("page_count", "Pages"),
    ("total_tokens", "Tokens"),
    ("estimated_cost_usd", "Est. cost"),
    ("elapsed_seconds", "Time"),
    ("skipped_count", "Skipped"),
)


def split_front_matter(text: str) -> tuple[dict, str]:
    """Split leading YAML front matter from the body.

    Only the flat `key: value` scalars our own writers emit are parsed; the
    body is returned unchanged if there is no front matter.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            meta = {}
            for line in lines[1:index]:
                key, separator, value = line.partition(":")
                if separator:
                    meta[key.strip()] = value.strip().strip('"')
            return meta, "\n".join(lines[index + 1:]).lstrip("\n")
    return {}, text


def _render_inline(text: str) -> str:
    """Escape a run of text, then apply inline markdown."""
    codes: list[str] = []

    def stash(match: re.Match) -> str:
        codes.append(match.group(2))
        return _CODE_TOKEN.format(len(codes) - 1)

    # Code spans are pulled out first so their contents never get emphasis
    # applied and never get re-interpreted as markup.
    text = _CODE_SPAN.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not SAFE_URL.match(url):
            return match.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    text = _LINK.sub(link, text)

    for index, code in enumerate(codes):
        text = text.replace(
            _CODE_TOKEN.format(index),
            f"<code>{html.escape(code.strip(), quote=False)}</code>",
        )
    return text


def _split_row(row: str) -> list[str]:
    """Split a markdown table row into cells, honoring escaped pipes."""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE.split(row)]


def _column_classes(divider: str) -> list[str]:
    """Read per-column alignment from a table's divider row."""
    classes = []
    for cell in _split_row(divider):
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            classes.append(" class=\"center\"")
        elif right:
            classes.append(" class=\"num\"")
        else:
            classes.append("")
    return classes


def _render_table(rows: list[str]) -> str:
    header, divider, body = rows[0], rows[1], rows[2:]
    classes = _column_classes(divider)

    def cells(row: str, tag: str) -> str:
        rendered = []
        for index, cell in enumerate(_split_row(row)):
            css = classes[index] if index < len(classes) else ""
            rendered.append(f"<{tag}{css}>{_render_inline(cell)}</{tag}>")
        return "".join(rendered)

    head = f"<thead><tr>{cells(header, 'th')}</tr></thead>"
    body_html = "".join(f"<tr>{cells(row, 'td')}</tr>" for row in body)
    return (
        '<div class="table-wrap"><table>'
        f"{head}<tbody>{body_html}</tbody>"
        "</table></div>"
    )


def _render_list(items: list[tuple[int, str, bool]]) -> str:
    """Render (indent, text, ordered) items, nesting by indentation."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []   # (indent, tag)
    for indent, text, ordered in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            if stack:
                out.append("")  # keep the parent <li> open for the nested list
            stack.append((indent, tag))
            out.append(f"<{tag}>")
        else:
            out.append("</li>")
        out.append(f"<li>{_render_inline(text)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """Convert the markdown subset used by our reports into HTML."""
    lines = text.split("\n")
    out: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), 4)
            out.append(f"<h{level}>{_render_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            index += 1
            continue

        # Table: a header row followed by a divider row.
        if "|" in stripped and index + 1 < len(lines) and _TABLE_DIVIDER.match(lines[index + 1]):
            rows = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            out.append(_render_table(rows))
            continue

        if _QUOTE.match(line):
            quoted = []
            while index < len(lines) and _QUOTE.match(lines[index]):
                quoted.append(_QUOTE.match(lines[index]).group(1))
                index += 1
            paragraphs = "".join(
                f"<p>{_render_inline(part.strip())}</p>"
                for part in "\n".join(quoted).split("\n\n")
                if part.strip()
            )
            out.append(f"<blockquote>{paragraphs}</blockquote>")
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            items = []
            while index < len(lines):
                bullet = _BULLET.match(lines[index])
                ordered = _ORDERED.match(lines[index])
                if bullet:
                    items.append((len(bullet.group(1)), bullet.group(2), False))
                elif ordered:
                    items.append((len(ordered.group(1)), ordered.group(2), True))
                else:
                    break
                index += 1
            out.append(_render_list(items))
            continue

        # Paragraph: consecutive plain lines up to the next blank or block.
        paragraph = []
        while index < len(lines):
            current = lines[index]
            if (
                not current.strip()
                or _HEADING.match(current)
                or _HR.match(current)
                or _QUOTE.match(current)
                or _BULLET.match(current)
                or _ORDERED.match(current)
                or (
                    "|" in current
                    and index + 1 < len(lines)
                    and _TABLE_DIVIDER.match(lines[index + 1])
                )
            ):
                break
            paragraph.append(current.strip())
            index += 1
        if paragraph:
            out.append(f"<p>{_render_inline(' '.join(paragraph))}</p>")

    return "\n".join(out)


def _format_meta_value(key: str, value: str) -> str:
    if key == "estimated_cost_usd":
        return f"${value}"
    if key == "elapsed_seconds":
        return f"{value}s"
    if key == "total_tokens":
        try:
            return f"{int(value):,}"
        except ValueError:
            return value
    return value


def _render_meta(meta: dict) -> str:
    chips = []
    for key, label in META_FIELDS:
        value = meta.get(key)
        if value in (None, "", "0") and key != "skipped_count":
            continue
        if key == "skipped_count" and value in (None, "", "0"):
            continue
        chips.append(
            f"<li>{html.escape(label)} <b>{html.escape(_format_meta_value(key, value))}</b></li>"
        )
    return f'<ul class="meta">{"".join(chips)}</ul>' if chips else ""


def render_page(markdown_text: str, title: str) -> str:
    """Render a full, self-contained HTML page from a report's markdown."""
    meta, body = split_front_matter(markdown_text)
    source = meta.get("source_file") or meta.get("source_files") or ""
    heading = source.strip("[]").split(",")[0].strip('" ') or title

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header class="masthead">
<p class="eyebrow">Asia Equity Analyzer</p>
<h1>{html.escape(heading)}</h1>
{_render_meta(meta)}
</header>
{markdown_to_html(body)}
</main>
</body>
</html>
"""


def export_markdown_file(md_path: str, output_path: str | None = None) -> str:
    """Render a report markdown file to a sibling .html file.

    Returns:
        Path to the written HTML file.
    """
    with open(md_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()

    if output_path is None:
        output_path = os.path.splitext(md_path)[0] + ".html"

    title = os.path.splitext(os.path.basename(md_path))[0]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(render_page(markdown_text, title))

    print(f"🌐 HTML written: {output_path}")
    return output_path
