"""HTML rendering, with the escaping rules treated as the main contract.

Report bodies are model-generated text derived from untrusted filings and the
resulting page is meant to be shared, so "nothing in a filing can execute in
the reader's browser" is the property these tests exist to protect.
"""

import unittest

from tests.support import install_stubs

install_stubs()

from utils.html_export import (  # noqa: E402
    markdown_to_html, render_page, split_front_matter,
)


class TestEscaping(unittest.TestCase):
    def test_script_tags_render_as_text(self):
        out = markdown_to_html("## Heading <script>alert(1)</script>\nbody")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_event_handler_attributes_are_neutralized(self):
        out = markdown_to_html("Text <img src=x onerror=alert(1)> more")
        assert "<img" not in out and "&lt;img" in out

    def test_raw_html_inside_table_cells_is_escaped(self):
        out = markdown_to_html("| A |\n|---|\n| <b>bold</b> |")
        assert "<b>bold</b>" not in out and "&lt;b&gt;" in out

    def test_javascript_urls_do_not_become_links(self):
        out = markdown_to_html("[click](javascript:alert(1))")
        assert 'href="javascript:' not in out
        assert "javascript:alert" in out  # still visible as plain text

    def test_data_urls_do_not_become_links(self):
        out = markdown_to_html("[x](data:text/html;base64,PHNjcmlwdD4=)")
        assert "<a href" not in out

    def test_safe_and_relative_links_work(self):
        out = markdown_to_html("[a](https://example.com) [b](./report.md) [c](mailto:x@y.z)")
        assert out.count("<a href=") == 3

    def test_code_span_contents_are_escaped_not_interpreted(self):
        out = markdown_to_html("Use `<script>bad()</script>` here")
        assert "<code>&lt;script&gt;bad()&lt;/script&gt;</code>" in out

    def test_full_page_has_no_external_references(self):
        page = render_page("## X\nbody", "t")
        assert "http://" not in page and "https://" not in page


class TestBlocks(unittest.TestCase):
    def test_headings_are_capped_at_h4(self):
        out = markdown_to_html("# a\n## b\n### c\n#### d\n##### e")
        assert "<h1>a</h1>" in out and "<h4>d</h4>" in out and "<h5>" not in out

    def test_table_alignment_comes_from_the_divider(self):
        out = markdown_to_html("| a | b | c |\n|---|--:|:-:|\n| 1 | 2 | 3 |")
        assert "<td>1</td>" in out
        assert '<td class="num">2</td>' in out
        assert '<td class="center">3</td>' in out

    def test_escaped_pipes_stay_inside_one_cell(self):
        # report_writer escapes literal pipes in filenames as \|
        out = markdown_to_html("| File | N |\n|---|---|\n| a \\| b.pdf | 1 |")
        assert "a | b.pdf" in out
        assert out.count("<td") == 2

    def test_nested_lists(self):
        out = markdown_to_html("- one\n- two\n  - nested\n- three")
        assert out.count("<ul>") == 2 and out.count("</ul>") == 2
        assert out.count("<li>") == 4

    def test_ordered_and_unordered_are_distinct(self):
        out = markdown_to_html("- a\n\n1. b")
        assert "<ul>" in out and "<ol>" in out

    def test_blockquote_and_rule(self):
        out = markdown_to_html("> quoted **text**\n\n---\n")
        assert "<blockquote><p>quoted <strong>text</strong></p></blockquote>" in out
        assert "<hr>" in out

    def test_paragraph_joins_wrapped_lines(self):
        out = markdown_to_html("one line\nsecond line\n\nnew para")
        assert "<p>one line second line</p>" in out and out.count("<p>") == 2

    def test_emphasis_leaves_ordinary_punctuation_alone(self):
        out = markdown_to_html("*em* and **strong** but 5 * 3 and snake_case_name")
        assert "<em>em</em>" in out and "<strong>strong</strong>" in out
        assert "5 * 3" in out and "snake_case_name" in out

    def test_empty_input_is_safe(self):
        assert markdown_to_html("") == ""
        assert render_page("", "title").startswith("<!doctype html>")


class TestFrontMatter(unittest.TestCase):
    REPORT = (
        '---\nsource_file: "Tencent FY2024.pdf"\nmodel: "claude-sonnet-4-6"\n'
        "total_tokens: 46303\nestimated_cost_usd: 0.2339\nelapsed_seconds: 24.3\n"
        "skipped_count: 0\n---\n\n## SECTION 1: X\nbody\n"
    )

    def test_split(self):
        meta, body = split_front_matter(self.REPORT)
        assert meta["source_file"] == "Tencent FY2024.pdf"
        assert meta["total_tokens"] == "46303"
        assert body.startswith("## SECTION 1")

    def test_absent_front_matter_returns_body_unchanged(self):
        meta, body = split_front_matter("## X\nbody")
        assert meta == {} and body == "## X\nbody"

    def test_metadata_becomes_header_chips(self):
        page = render_page(self.REPORT, "fallback")
        assert "<h1>Tencent FY2024.pdf</h1>" in page
        assert "Tokens <b>46,303</b>" in page
        assert "Est. cost <b>$0.2339</b>" in page
        assert "Time <b>24.3s</b>" in page

    def test_zero_valued_skip_chip_is_omitted(self):
        assert "Skipped" not in render_page(self.REPORT, "t")

    def test_title_falls_back_to_filename(self):
        page = render_page("## Only A Section\nbody", "my-report")
        assert "<h1>my-report</h1>" in page

    def test_theme_and_print_rules_are_present(self):
        page = render_page(self.REPORT, "t")
        assert "prefers-color-scheme: dark" in page
        assert "@media print" in page


if __name__ == "__main__":
    unittest.main()
