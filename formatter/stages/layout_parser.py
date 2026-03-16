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
    Extract text from DOCX preserving:
      - Author blocks (multi-line paragraphs with \\n inside)
      - Equations in [\\latex] or [\\n\\latex\\n] format → converted to $$...$$ display math
      - Tables → reconstructed as pipe-delimited text with a Table caption marker
      - Subsection headings (A. Title, B. Title)
      - Normal paragraphs separated by blank lines
    """
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    import re

    doc = DocxDocument(path)

    # Build a map of table positions so we can insert them inline
    # (python-docx tables appear in doc.tables but also in the XML body)
    table_texts = {}
    for i, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            rows.append(cells)
        if rows:
            # First row = header
            n_cols = max(len(r) for r in rows)
            header = rows[0]
            sep    = ["-" * max(3, len(h)) for h in header]
            lines  = [" | ".join((header + [""] * n_cols)[:n_cols])]
            lines += [" | ".join((sep   + ["---"] * n_cols)[:n_cols])]
            for row in rows[1:]:
                lines.append(" | ".join((row + [""] * n_cols)[:n_cols]))
            table_texts[i] = "\n".join(lines)

    # Walk body XML to get paragraphs and tables in document order
    body = doc.element.body
    para_idx  = 0
    table_idx = 0
    output_blocks: list[str] = []

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            # Normal paragraph
            if para_idx < len(doc.paragraphs):
                para = doc.paragraphs[para_idx]
                para_idx += 1
                text = para.text.strip()
                if not text:
                    continue

                # ── Equation block: [\n\\latex\n] or [\\latex] ────────────────
                eq_match = re.match(r"^\[\s*\n?(.*?)\n?\s*\]$", text, re.DOTALL)
                if eq_match:
                    latex = eq_match.group(1).strip()
                    # Unescape doubled backslashes from DOCX string encoding
                    latex = latex.replace("\\\\", "\\")
                    output_blocks.append(f"$$\n{latex}\n$$")
                    continue

                # ── Multi-author block: contains \n (soft line breaks) ────────
                if "\n" in text and para_idx <= 5:
                    # Keep as-is — document_parser handles multi-line author blocks
                    output_blocks.append(text)
                    continue

                output_blocks.append(text)

        elif tag == "tbl":
            if table_idx < len(doc.tables):
                tbl = doc.tables[table_idx]
                table_idx += 1

                # Find caption: look at preceding paragraph
                caption = ""
                # Look back in output_blocks for a short title-like block
                for prev in reversed(output_blocks[-3:]):
                    if len(prev.split()) <= 6 and not prev.startswith("$$"):
                        caption = prev
                        output_blocks.remove(prev)
                        break

                rows = []
                for row in tbl.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    rows.append(cells)

                if rows:
                    n_cols  = max(len(r) for r in rows)
                    header  = (rows[0] + [""] * n_cols)[:n_cols]
                    tbl_lines = [" & ".join(header)]
                    for row in rows[1:]:
                        tbl_lines.append(" & ".join((row + [""] * n_cols)[:n_cols]))
                    # Emit as a special TABLE block that document_parser can reconstruct
                    cap_text = caption if caption else f"Table {table_idx}"
                    output_blocks.append(
                        "DOCX_TABLE_START\n" +
                        "\n".join(tbl_lines) +
                        f"\nTable {table_idx}: {cap_text}\nDOCX_TABLE_END"
                    )

    return "\n\n".join(output_blocks)