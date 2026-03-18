"""
extractor/pdf_extractor.py — Stage 1: PDF → raw text + font-aware blocks + tables.

Uses:
  - pymupdf (fitz) for text with font metadata (size, bold)
  - pdfplumber for table detection
"""
import os, json, re
from core.logger import get_logger

log = get_logger(__name__)


def extract(pdf_path: str, inter_dir: str) -> dict:
    """
    Returns:
      {
        "raw_text":  str,             # plain text of entire PDF
        "blocks":    list[dict],      # [{text, font_size, bold, page}, ...]
        "tables":    list[dict],      # [{caption, headers, rows}, ...]
        "images":    list[str],       # saved image paths
      }
    """
    os.makedirs(inter_dir, exist_ok=True)
    blocks = _extract_blocks(pdf_path, inter_dir)
    raw_text = _blocks_to_text(blocks)
    tables   = _extract_tables(pdf_path)
    images   = []   # image extraction omitted for simplicity

    # Write raw text for inspection
    txt_path = os.path.join(inter_dir, "extracted.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(raw_text)

    log.info("PDF extraction: %d blocks, %d chars, %d tables",
             len(blocks), len(raw_text), len(tables))
    return {"raw_text": raw_text, "blocks": blocks, "tables": tables, "images": images}


def _extract_blocks(pdf_path: str, inter_dir: str) -> list:
    """Extract text blocks with font size and bold flag using pymupdf."""
    try:
        import fitz  # pymupdf
    except ImportError:
        log.warning("pymupdf not installed — falling back to plain text extraction")
        return _fallback_blocks(pdf_path)

    blocks = []
    doc = fitz.open(pdf_path)

    for page_num, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:   # 0 = text, 1 = image
                continue
            lines_text = []
            font_sizes = []
            is_bold    = False
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if t:
                        lines_text.append(t)
                        font_sizes.append(span.get("size", 10))
                        # bold flag is bit 4 (0b10000 = 16) of flags int
                        if span.get("flags", 0) & 16:
                            is_bold = True

            if not lines_text:
                continue

            text = " ".join(lines_text).strip()
            if not text:
                continue

            avg_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10
            blocks.append({
                "text":      text,
                "font_size": round(avg_size, 2),
                "bold":      is_bold,
                "page":      page_num,
            })

    doc.close()
    return blocks


def _fallback_blocks(pdf_path: str) -> list:
    """
    Fallback when pymupdf isn't installed: extract per-line blocks
    from pdfplumber with page numbers. No font info available.
    """
    try:
        import pdfplumber
    except ImportError:
        log.error("Neither pymupdf nor pdfplumber installed — cannot extract PDF")
        return []

    blocks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    blocks.append({
                        "text":      line,
                        "font_size": 10,    # unknown without pymupdf
                        "bold":      False,
                        "page":      page_num,
                    })

    log.info("Fallback extraction: %d line-blocks from %d pages",
             len(blocks), blocks[-1]["page"] if blocks else 0)
    return blocks


def _blocks_to_text(blocks: list) -> str:
    """Join blocks into clean plain text with double-newline separators."""
    parts = []
    for b in blocks:
        t = b["text"].strip()
        if t:
            parts.append(t)
    text = "\n\n".join(parts)
    # Strip common PDF noise: page numbers, running headers
    text = re.sub(r"^\s*\d{1,4}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_tables(pdf_path: str) -> list:
    """Extract tables using pdfplumber."""
    tables = []
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for tbl in (page.extract_tables() or []):
                    if not tbl:
                        continue
                    headers = [str(c or "").strip() for c in tbl[0]]
                    rows    = [[str(c or "").strip() for c in row] for row in tbl[1:]]
                    if any(h for h in headers):
                        tables.append({"caption": "", "headers": headers, "rows": rows, "notes": ""})
    except Exception as e:
        log.warning("Table extraction failed: %s", e)
    return tables
