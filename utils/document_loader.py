"""Document loader: scans input/ for PDF or text files and extracts content."""

from __future__ import annotations

import os
from dataclasses import dataclass

import fitz  # pymupdf

from config import INPUT_DIR, MAX_DOCUMENT_CHARS


@dataclass
class DocumentInfo:
    """Metadata and extracted text for a loaded document."""

    text: str
    filename: str
    filepath: str
    page_count: int      # 0 for plain-text files
    original_chars: int  # character count before any truncation
    was_truncated: bool


def get_most_recent_file() -> tuple[str, str] | None:
    """Find the most recent .pdf or .txt file in the input directory.

    Returns:
        Tuple of (filepath, filename) or None if no valid file found.
    """
    if not os.path.isdir(INPUT_DIR):
        print(f"❌ Input directory '{INPUT_DIR}' does not exist.")
        return None

    valid_extensions = {".pdf", ".txt"}
    candidates = []

    for fname in os.listdir(INPUT_DIR):
        ext = os.path.splitext(fname)[1].lower()
        if ext in valid_extensions:
            fpath = os.path.join(INPUT_DIR, fname)
            mtime = os.path.getmtime(fpath)
            candidates.append((fpath, fname, mtime))

    if not candidates:
        print(f"❌ No .pdf or .txt files found in '{INPUT_DIR}/'.")
        return None

    # Sort by modification time, most recent first
    candidates.sort(key=lambda x: x[2], reverse=True)
    fpath, fname, _ = candidates[0]
    return fpath, fname


def extract_text_from_pdf(filepath: str) -> tuple[str, int]:
    """Extract text from a PDF file using pymupdf.

    Returns:
        Tuple of (extracted_text, page_count).
    """
    doc = fitz.open(filepath)
    try:
        page_count = len(doc)
        text_parts = []
        for page_num, page in enumerate(doc, start=1):
            page_text = page.get_text()
            if page_text.strip():
                text_parts.append(f"--- Page {page_num} ---\n{page_text}")
        return "\n\n".join(text_parts), page_count
    finally:
        doc.close()


def extract_text_from_txt(filepath: str) -> str:
    """Read text from a plain text file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def load_document(filepath: str | None = None) -> DocumentInfo | None:
    """Load a document for analysis.

    Args:
        filepath: Explicit path to a PDF or .txt file. When None, the most
                  recent file in the configured input directory is used.

    Returns:
        DocumentInfo on success, or None on failure.
    """
    if filepath is not None:
        if not os.path.isfile(filepath):
            print(f"❌ File not found: '{filepath}'")
            return None
        resolved_path = filepath
        filename = os.path.basename(filepath)
    else:
        result = get_most_recent_file()
        if result is None:
            return None
        resolved_path, filename = result

    ext = os.path.splitext(filename)[1].lower()
    print(f"📄 Loading: {filename}")

    try:
        if ext == ".pdf":
            text, page_count = extract_text_from_pdf(resolved_path)
        elif ext == ".txt":
            text = extract_text_from_txt(resolved_path)
            page_count = 0
        else:
            print(f"❌ Unsupported file type: {ext}")
            return None
    except Exception as e:
        print(f"❌ Failed to extract text from '{filename}': {e}")
        return None

    if not text.strip():
        print(f"⚠️  Warning: Extracted text from '{filename}' is empty.")
        return None

    original_chars = len(text)
    print(f"   Characters extracted: {original_chars:,}")
    if page_count:
        print(f"   Pages: {page_count:,}")

    was_truncated = original_chars > MAX_DOCUMENT_CHARS
    if was_truncated:
        print(
            f"⚠️  Warning: Document exceeds {MAX_DOCUMENT_CHARS:,} character limit. "
            f"Truncating from {original_chars:,} to {MAX_DOCUMENT_CHARS:,} characters."
        )
        print(
            "   Later sections of the document (financial statements, appendices) "
            "may be cut. This could affect balance sheet and cash flow analysis."
        )
        text = text[:MAX_DOCUMENT_CHARS]

    return DocumentInfo(
        text=text,
        filename=filename,
        filepath=resolved_path,
        page_count=page_count,
        original_chars=original_chars,
        was_truncated=was_truncated,
    )
