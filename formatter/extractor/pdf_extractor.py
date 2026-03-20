"""
extractor/pdf_extractor.py — Stage 1: PDF -> raw text + font-aware blocks + tables + images.

RULE: PyMuPDF (fitz) is the PRIMARY text extractor.
      pdfplumber is used ONLY for table detection and image extraction.
      Springer PDFs use CID/Adobe-Identity-UCS encoding that causes
      pdfplumber to silently return garbled text or nothing.
"""
import os
import re

from core.logger import get_logger

log = get_logger(__name__)


def extract(pdf_path: str, inter_dir: str) -> dict:
    """
    Returns:
      {
        "raw_text":  str,             # plain text of entire PDF
        "blocks":    list[dict],      # [{text, font_size, bold, page}, ...]
        "tables":    list[dict],      # [{caption, headers, rows, notes, page}, ...]
        "images":    list[dict],      # [{path, filename, page, width, height}, ...]
      }
    """
    os.makedirs(inter_dir, exist_ok=True)

    blocks   = _extract_blocks(pdf_path)
    raw_text = _blocks_to_text(blocks)
    tables   = _extract_tables(pdf_path)
    images   = _extract_images(pdf_path, inter_dir)

    # Write raw text for inspection
    txt_path = os.path.join(inter_dir, "extracted.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(raw_text)

    log.info("PDF extraction: %d blocks, %d chars, %d tables, %d images",
             len(blocks), len(raw_text), len(tables), len(images))
    return {
        "raw_text": raw_text,
        "blocks":   blocks,
        "tables":   tables,
        "images":   images,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Block extraction — PyMuPDF primary, pdfplumber fallback
# ══════════════════════════════════════════════════════════════════════════════

def _extract_blocks(pdf_path: str) -> list:
    """Extract text blocks with font size and bold flag using PyMuPDF."""
    try:
        import fitz  # pymupdf
    except ImportError:
        log.warning("pymupdf not installed — falling back to pdfplumber line blocks")
        return _fallback_blocks(pdf_path)

    blocks = []
    doc = fitz.open(pdf_path)

    for page_num, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:  # 0 = text block
                continue
            lines_text = []
            font_sizes = []
            is_bold = False
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if t:
                        lines_text.append(t)
                        font_sizes.append(span.get("size", 10))
                        # Bold flag is bit 4 (0b10000 = 16) of the flags int
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
    """Fallback: pdfplumber per-line blocks (no font info).

    Detects missing-spaces font encoding (avg word length > 12) and
    switches to char-position-based space recovery in that case.
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

            # Detect space-stripped encoding: if average word length is
            # suspiciously large, words are concatenated without spaces.
            if text:
                words = text.split()
                avg_word_len = (sum(len(w) for w in words) / len(words)
                                if words else 0)
                if avg_word_len > 12:
                    log.info("Page %d: avg word len %.1f > 12 — using "
                             "char-based space recovery", page_num, avg_word_len)
                    text = _recover_spaces_from_chars(page)

            for line in text.split("\n"):
                line = line.strip()
                if line:
                    blocks.append({
                        "text":      line,
                        "font_size": 10,    # unknown
                        "bold":      False,
                        "page":      page_num,
                    })

    log.info("Fallback extraction: %d line-blocks from %d pages",
             len(blocks), blocks[-1]["page"] if blocks else 0)
    return blocks


def _recover_spaces_from_chars(page) -> str:
    """Reconstruct text with proper word spacing from character x-positions.

    When a PDF font omits space characters, adjacent words appear concatenated
    in extract_text() output.  This function reads page.chars (which always
    has accurate glyph bounding boxes) and inserts a space wherever the gap
    between consecutive characters on the same line exceeds 40% of the average
    character width for that line.

    Returns a newline-separated string, one visual line per line.
    """
    chars = page.chars
    if not chars:
        return ""

    from collections import defaultdict

    # Group characters by rounded y-position (2-pt tolerance keeps same-line
    # chars together while separating distinct text rows).
    lines: dict = defaultdict(list)
    for c in chars:
        y_key = round(c["top"] / 2) * 2
        lines[y_key].append(c)

    result_lines = []
    for y in sorted(lines.keys()):
        line_chars = sorted(lines[y], key=lambda c: c["x0"])
        if not line_chars:
            continue

        # Average character width for dynamic threshold (handles mixed font sizes)
        widths = [c["x1"] - c["x0"] for c in line_chars if c["x1"] > c["x0"]]
        avg_width = sum(widths) / len(widths) if widths else 5.0
        # Space gap threshold: 40% of avg char width (chosen from data: within-word
        # kerning gaps are 0–0.1 pt, word-boundary gaps are ~2.4 pt for 11 pt font)
        threshold = max(avg_width * 0.40, 1.0)

        line_text = line_chars[0]["text"]
        for i in range(1, len(line_chars)):
            gap = line_chars[i]["x0"] - line_chars[i - 1]["x1"]
            if gap > threshold:
                line_text += " "
            line_text += line_chars[i]["text"]

        line_text = line_text.strip()
        if line_text:
            result_lines.append(line_text)

    return "\n".join(result_lines)


def _blocks_to_text(blocks: list) -> str:
    """Join blocks into plain text with double-newline separators."""
    parts = [b["text"].strip() for b in blocks if b["text"].strip()]
    text = "\n\n".join(parts)
    # Strip stray page-number lines and Springer "N N" markers
    text = re.sub(r"^\s*\d{1,4}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d{1,4}\s+\d{1,4}\s*$", "", text, flags=re.MULTILINE)
    # Strip "72 Page 2 of 36" / "Page 3 of 36 72" patterns
    text = re.sub(r"^\s*\d*\s*Page\s+\d+\s+of\s+\d+\s*\d*\s*$", "", text,
                  flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Table extraction — pdfplumber ONLY
# ══════════════════════════════════════════════════════════════════════════════

def _extract_tables(pdf_path: str) -> list:
    """Extract tables using pdfplumber (line-based strategy)."""
    tables = []
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                for tbl in (page.extract_tables() or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    headers = [str(c or "").strip() for c in tbl[0]]
                    rows = [[str(c or "").strip() for c in row] for row in tbl[1:]]
                    if any(h for h in headers):
                        tables.append({
                            "caption": "",
                            "headers": headers,
                            "rows":    rows,
                            "notes":   "",
                            "page":    page_num,
                        })
    except Exception as e:
        log.warning("Table extraction failed: %s", e)
    log.info("Extracted %d tables from PDF", len(tables))
    return tables


# ══════════════════════════════════════════════════════════════════════════════
# Image extraction — pdfplumber ONLY
# ══════════════════════════════════════════════════════════════════════════════

def _extract_images(pdf_path: str, inter_dir: str) -> list:
    """Extract images by cropping pdfplumber page regions."""
    images = []
    img_dir = os.path.join(inter_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            img_idx = 0
            for page_num, page in enumerate(pdf.pages, start=1):
                for im in (page.images or []):
                    w = im.get("width", 0)
                    h = im.get("height", 0)
                    # Skip tiny images (logos, line decorations)
                    if w < 100 or h < 50:
                        continue
                    img_idx += 1
                    try:
                        bbox = (im["x0"], im["top"], im["x1"], im["bottom"])
                        cropped = page.crop(bbox)
                        rendered = cropped.to_image(resolution=200)
                        filename = f"fig_{img_idx}_p{page_num}.png"
                        filepath = os.path.join(img_dir, filename)
                        rendered.save(filepath)
                        images.append({
                            "path":     filepath,
                            "filename": filename,
                            "page":     page_num,
                            "width":    w,
                            "height":   h,
                        })
                    except Exception as e:
                        log.warning("Failed to extract image %d from page %d: %s",
                                    img_idx, page_num, e)
    except Exception as e:
        log.warning("Image extraction failed: %s", e)

    log.info("Extracted %d images from PDF", len(images))
    return images
