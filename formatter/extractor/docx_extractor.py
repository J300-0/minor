"""
extractor/docx_extractor.py — Stage 1: DOCX → raw text + blocks + tables.
Uses python-docx.
"""
import os
from core.logger import get_logger

log = get_logger(__name__)


def extract(docx_path: str, inter_dir: str) -> dict:
    os.makedirs(inter_dir, exist_ok=True)
    try:
        import docx as python_docx
    except ImportError:
        log.error("python-docx not installed — run: pip install python-docx")
        return {"raw_text": "", "blocks": [], "tables": [], "images": []}

    doc   = python_docx.Document(docx_path)
    blocks = []
    tables = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        # Detect bold / heading style
        is_bold  = any(run.bold for run in para.runs if run.text.strip())
        is_head  = para.style.name.startswith("Heading")
        font_size = 12
        for run in para.runs:
            if run.font.size:
                font_size = run.font.size.pt
                break
        blocks.append({
            "text":      text,
            "font_size": font_size if not is_head else 14,
            "bold":      is_bold or is_head,
            "page":      0,
        })

    for tbl in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in tbl.rows]
        if rows:
            tables.append({"caption": "", "headers": rows[0], "rows": rows[1:], "notes": ""})

    raw_text = "\n\n".join(b["text"] for b in blocks)
    txt_path = os.path.join(inter_dir, "extracted.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(raw_text)

    log.info("DOCX extraction: %d blocks, %d chars, %d tables",
             len(blocks), len(raw_text), len(tables))
    return {"raw_text": raw_text, "blocks": blocks, "tables": tables, "images": []}
