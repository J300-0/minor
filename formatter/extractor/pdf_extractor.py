"""
extractor/pdf_extractor.py  —  PDF → {raw_text, tables, images}

Text extraction:  PyMuPDF (fitz) — primary, handles CID/Springer fonts reliably
Table extraction: pdfplumber — native table detection via line strategy
Image extraction: PyMuPDF (fitz)

Why PyMuPDF for text (not pdfplumber):
  pdfplumber uses pdfminer under the hood which struggles with CID-encoded fonts
  (Adobe-Identity-UCS) common in Springer/Elsevier publisher PDFs.
  This causes extraction to silently return ~3K chars instead of ~92K.
  PyMuPDF handles these fonts correctly and consistently returns full text.
"""

import os, re, json
from core.logger import get_logger

log = get_logger(__name__)


def extract(input_path: str, intermediate_dir: str) -> dict:
    """Always returns {raw_text, tables, images} — never raises."""
    result = {"raw_text": "", "tables": [], "images": []}
    try:
        result = _extract(input_path, intermediate_dir)
    except Exception as e:
        log.error(f"PDF extraction failed: {e}", exc_info=True)
        print(f"         [extractor] failed: {e}")

    _write_outputs(result, intermediate_dir)
    print(f"         [pdf] {len(result['raw_text'])} chars | "
          f"{len(result['tables'])} tables | {len(result['images'])} images")
    log.info(f"extracted: {len(result['raw_text'])} chars | "
             f"{len(result['tables'])} tables | {len(result['images'])} images")
    return result


def _extract(input_path: str, intermediate_dir: str) -> dict:
    import fitz  # PyMuPDF

    figures_dir = os.path.join(intermediate_dir, "figures")
    os.makedirs(figures_dir, exist_ok=True)

    # ── Text + images via PyMuPDF ─────────────────────────────────────────────
    text_pages = []
    images     = []
    fig_count  = 0

    doc = fitz.open(input_path)
    for page_num, page in enumerate(doc):
        # Text — PyMuPDF handles CID/Springer fonts correctly
        text = page.get_text("text")
        if text.strip():
            text_pages.append(text)

        # Images
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base  = doc.extract_image(xref)
                data  = base["image"]
                if len(data) < 5000:
                    continue
                fig_count += 1
                ext   = base["ext"]
                fpath = os.path.join(figures_dir, f"fig_{fig_count}.{ext}")
                with open(fpath, "wb") as f:
                    f.write(data)
                rects = page.get_image_rects(xref)
                images.append({
                    "path":  fpath,
                    "page":  page_num + 1,
                    "y_pos": int(rects[0].y0) if rects else 0,
                })
            except Exception:
                continue
    doc.close()

    raw_text = "\n\n".join(text_pages)
    log.debug(f"PyMuPDF extracted {len(raw_text)} chars, {fig_count} images")

    # ── Tables via pdfplumber (line strategy) ─────────────────────────────────
    tables = _extract_tables_pdfplumber(input_path)
    if tables:
        _match_captions(raw_text, tables)
        log.info(f"pdfplumber found {len(tables)} tables")
    else:
        log.warning("pdfplumber found 0 tables — paper may use image-based tables")

    return {"raw_text": raw_text, "tables": tables, "images": images}


def _extract_tables_pdfplumber(path: str) -> list:
    """Extract tables only — isolated so a pdfplumber crash doesn't kill text."""
    try:
        import pdfplumber
        tables = []
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                page_tables = page.extract_tables({
                    "vertical_strategy":   "lines",
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 5,
                    "join_tolerance":  5,
                })
                for tbl in (page_tables or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    clean = [[str(c).strip() if c else "" for c in row] for row in tbl]
                    tables.append({
                        "caption": "", "headers": clean[0],
                        "rows": clean[1:], "page": page_num, "notes": "",
                    })
        return tables
    except Exception as e:
        log.warning(f"pdfplumber table extraction failed: {e}")
        return []


def _match_captions(text: str, tables: list):
    caps = re.findall(r"(Table\s+\d+[^\.\n]*(?:\.[^\.\n]*)?)", text, re.IGNORECASE)
    for i, tbl in enumerate(tables):
        if i < len(caps):
            tbl["caption"] = caps[i].strip()


def _write_outputs(result: dict, intermediate_dir: str):
    os.makedirs(intermediate_dir, exist_ok=True)
    with open(os.path.join(intermediate_dir, "extracted.txt"), "w", encoding="utf-8") as f:
        f.write(result["raw_text"])
    with open(os.path.join(intermediate_dir, "extracted_rich.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)