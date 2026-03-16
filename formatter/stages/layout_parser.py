"""
stages/layout_parser.py
Stage 1 — PDF/DOCX → raw text blocks (extracted.txt)

Responsibility:
  - Detect file type
  - Extract text preserving paragraph/block boundaries
  - Write to a plain .txt file (one paragraph per double-newline)

Does NOT interpret structure — that's document_parser's job.
"""

import os
import fitz                         # PyMuPDF


def parse(input_path: str, output_path: str) -> str:
    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".pdf":
        text = _from_pdf(input_path)
    elif ext in (".docx", ".doc"):
        text = _from_docx(input_path)
    else:
        raise ValueError(f"Unsupported file type '{ext}'. Expected .pdf or .docx")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"         → {output_path}")
    return output_path


def _from_pdf(path: str) -> str:
    """
    Use PyMuPDF's block-level extraction.
    Each block becomes a paragraph (double-newline separated).
    Sorts blocks top-to-bottom, left-to-right to handle multi-column layouts.
    """
    doc = fitz.open(path)
    paragraphs = []

    for page in doc:
        blocks = page.get_text("blocks")          # list of (x0,y0,x1,y1,text,block_no,type)
        # Sort by vertical position then horizontal (handles 2-column PDFs)
        blocks.sort(key=lambda b: (round(b[1] / 20) * 20, b[0]))
        for block in blocks:
            text = block[4].strip()
            if text:
                paragraphs.append(text)

    return "\n\n".join(paragraphs)


def _from_docx(path: str) -> str:
    """
    python-docx paragraph extraction.
    Consecutive non-empty paragraphs are joined; blank paragraphs act as separators.
    """
    from docx import Document

    doc = Document(path)
    groups = []
    current = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            current.append(text)
        else:
            if current:
                groups.append(" ".join(current))
                current = []

    if current:
        groups.append(" ".join(current))

    return "\n\n".join(groups)