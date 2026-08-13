"""Document discovery, and real PDF extraction where the library is present.

Every other test in the suite stubs pymupdf, so the actual extraction path
is only covered here — and only when the real package is installed.
"""

import os
import time
import unittest

from tests.support import has_real_pymupdf, install_stubs, quiet, temp_workspace

install_stubs()

REAL_PYMUPDF = has_real_pymupdf()

from utils.document_loader import find_documents, load_document  # noqa: E402


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        temp_workspace()
        os.makedirs("inbox")

    def touch(self, name, body="x"):
        with open(os.path.join("inbox", name), "w") as f:
            f.write(body)

    def test_lists_only_supported_extensions_sorted_by_name(self):
        for name in ("b.pdf", "a.txt", "c.PDF", "notes.md", "data.csv"):
            self.touch(name)
        with quiet():
            found = find_documents("inbox")
        assert [os.path.basename(p) for p in found] == ["a.txt", "b.pdf", "c.PDF"]

    def test_subdirectories_are_not_treated_as_filings(self):
        os.makedirs("inbox/archive.pdf")
        self.touch("real.txt")
        with quiet():
            found = find_documents("inbox")
        assert [os.path.basename(p) for p in found] == ["real.txt"]

    def test_missing_and_empty_directories_return_none(self):
        with quiet() as out:
            assert find_documents("nope") is None
            assert find_documents("inbox") is None
        assert "does not exist" in out.getvalue()
        assert "No .pdf or .txt files" in out.getvalue()


class TestTextLoading(unittest.TestCase):
    def setUp(self):
        temp_workspace()

    def test_reads_a_text_filing(self):
        with open("filing.txt", "w") as f:
            f.write("Revenue grew 12%.")
        with quiet():
            info = load_document(filepath="filing.txt")
        assert info.text == "Revenue grew 12%."
        assert info.page_count == 0 and not info.was_truncated

    def test_truncates_oversized_filings_and_says_so(self):
        from config import MAX_DOCUMENT_CHARS

        with open("big.txt", "w") as f:
            f.write("x" * (MAX_DOCUMENT_CHARS + 500))
        with quiet() as out:
            info = load_document(filepath="big.txt")
        assert info.was_truncated
        assert len(info.text) == MAX_DOCUMENT_CHARS
        assert info.original_chars == MAX_DOCUMENT_CHARS + 500
        assert "Truncating" in out.getvalue()

    def test_missing_file_and_unsupported_type_return_none(self):
        with open("notes.md", "w") as f:
            f.write("x")
        with quiet():
            assert load_document(filepath="absent.txt") is None
            assert load_document(filepath="notes.md") is None

    def test_empty_filing_is_rejected(self):
        with open("blank.txt", "w") as f:
            f.write("   \n  ")
        with quiet() as out:
            assert load_document(filepath="blank.txt") is None
        assert "is empty" in out.getvalue()


@unittest.skipUnless(REAL_PYMUPDF, "pymupdf not installed")
class TestPdfExtraction(unittest.TestCase):
    """The one path the stubs cannot cover."""

    def setUp(self):
        temp_workspace()

    def write_pdf(self, name, pages):
        import pymupdf

        doc = pymupdf.open()
        for body in pages:
            page = doc.new_page()
            page.insert_text((72, 100), body)
        doc.save(name)
        doc.close()

    def test_extracts_text_and_page_count(self):
        self.write_pdf("filing.pdf", ["Revenue grew 12% to HKD 4,182m.",
                                      "Net cash of HKD 1,942m."])
        with quiet():
            info = load_document(filepath="filing.pdf")
        assert info.page_count == 2
        assert "Revenue grew 12%" in info.text
        assert "Net cash" in info.text

    def test_pages_are_labelled_for_the_model(self):
        self.write_pdf("filing.pdf", ["first", "second"])
        with quiet():
            info = load_document(filepath="filing.pdf")
        assert "--- Page 1 ---" in info.text and "--- Page 2 ---" in info.text

    def test_corrupt_pdf_is_reported_not_raised(self):
        with open("broken.pdf", "wb") as f:
            f.write(b"NOT A PDF")
        with quiet() as out:
            assert load_document(filepath="broken.pdf") is None
        assert "Failed to extract text" in out.getvalue()

    def test_image_only_pdf_yields_no_text(self):
        # A scanned filing with no text layer must be rejected rather than
        # sent to the API as an empty document.
        import pymupdf

        doc = pymupdf.open()
        doc.new_page()
        doc.save("scanned.pdf")
        doc.close()
        with quiet() as out:
            assert load_document(filepath="scanned.pdf") is None
        assert "is empty" in out.getvalue()


if __name__ == "__main__":
    unittest.main()
