"""Filename sanitising and markdown-safety in the report writers.

These are regression tests for bugs where a perfectly ordinary filing name
destroyed output: a name too long to write, or one that collapsed into a key
shared with unrelated companies.
"""

import os
import unittest

from tests.support import install_stubs, quiet, temp_workspace

install_stubs()

from utils.report_writer import MAX_NAME_BYTES, sanitize_filename, write_report  # noqa: E402


class TestSanitizeFilename(unittest.TestCase):
    def test_ordinary_names_are_lowercased_and_underscored(self):
        assert sanitize_filename("Tencent FY2024.pdf") == "tencent_fy2024"
        assert sanitize_filename("Report: Q3 (final).txt") == "report_q3_final"

    def test_name_with_no_word_characters_falls_back(self):
        assert sanitize_filename("----.pdf") == "report"
        assert sanitize_filename("   ") == "report"

    def test_dotfiles_keep_their_name(self):
        # splitext treats a leading dot as a hidden file rather than an
        # extension, so ".pdf" is a name, not an empty one. Such a file is
        # rejected by the loader anyway (it has no recognised extension).
        assert sanitize_filename(".pdf") == "pdf"

    def test_cjk_names_stay_distinct(self):
        # Regression: stripping to ASCII made every "<company>2024.pdf" collapse
        # to "2024", so unrelated companies overwrote each other's reports and
        # merged into a single watchlist history.
        names = ["腾讯控股年报2024.pdf", "阿里巴巴年报2024.pdf",
                 "네이버_2024.pdf", "サムスン2024.pdf"]
        keys = [sanitize_filename(n) for n in names]
        assert len(set(keys)) == len(names), keys
        assert all(k != "2024" for k in keys), keys

    def test_long_names_are_truncated_to_a_writable_length(self):
        # Regression: an over-long name raised OSError from write_report,
        # discarding an analysis that had already been paid for.
        assert len(sanitize_filename("A" * 300 + ".pdf").encode()) <= MAX_NAME_BYTES

    def test_truncation_counts_bytes_not_characters(self):
        # CJK characters are three UTF-8 bytes; a character-based cap would
        # still overflow the filesystem's byte limit.
        long_cjk = sanitize_filename("腾" * 200 + ".pdf")
        assert len(long_cjk.encode()) <= MAX_NAME_BYTES
        assert long_cjk  # and it does not truncate into an empty string

    def test_truncation_does_not_split_a_character(self):
        sanitize_filename("腾" * 200 + ".pdf").encode("utf-8").decode("utf-8")


class TestWriteReport(unittest.TestCase):
    def setUp(self):
        temp_workspace()

    def write(self, filename):
        with quiet():
            return write_report(
                analysis_text="## SECTION 1: X\nbody", source_filename=filename,
                input_tokens=1, output_tokens=1, elapsed_seconds=1.0, estimated_cost=0.1,
            )

    def test_pathological_filenames_still_produce_a_file(self):
        for name in ("A" * 300 + ".pdf", "腾" * 200 + ".pdf", "腾讯控股年报2024.pdf",
                     "----.pdf", "Report | With Pipe.pdf", "Report: FY2024.pdf"):
            path = self.write(name)
            assert os.path.exists(path), name
            assert len(os.path.basename(path).encode()) <= 255, name

    def test_source_filename_survives_intact_in_the_report(self):
        # The sanitized name is only for the output path; the report itself
        # must still identify the filing it came from.
        path = self.write("腾讯控股年报2024.pdf")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        assert "腾讯控股年报2024.pdf" in body

    def test_markdown_metacharacters_in_filenames_are_escaped(self):
        path = self.write("Report | With Pipe.pdf")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        stats = [line for line in body.splitlines() if line.startswith("| Source File")][0]
        assert r"\|" in stats


if __name__ == "__main__":
    unittest.main()
