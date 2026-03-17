"""
extractor/pdf_extractor.py  —  PDF → {raw_text, tables, images}

Uses pdfplumber (from PDF skill) for text + native table extraction.
Uses PyMuPDF (fitz) for image extraction only.

Returns a rich dict always — never None or raises silently.
Per the PDF skill: pdfplumber is the recommended tool for text/table extraction.
"""

import os, re, json


def extract(input_path: str, intermediate_dir: str) -> dict:
    """
    Extract text, tables, and images from a PDF.
    Always returns: {"raw_text": str, "tables": list, "images": list}
    Writes extracted.txt and extracted_rich.json to intermediate_dir.
    """
    result = {"raw_text": "", "tables": [], "images": []}

    try:
        result = _extract_with_pdfplumber(input_path, intermediate_dir)
    except Exception as e:
        print(f"         [extractor] pdfplumber failed: {e} — trying pypdf fallback")
        try:
            result = _extract_with_pypdf(input_path)
        except Exception as e2:
            print(f"         [extractor] pypdf fallback also failed: {e2}")
            # Return empty-but-valid dict so pipeline continues
            result = {"raw_text": "", "tables": [], "images": []}

    _write_outputs(result, intermediate_dir)
    print(f"         [pdf] {len(result['raw_text'])} chars | "
          f"{len(result['tables'])} tables | {len(result['images'])} images")
    return result


# ── pdfplumber path (primary) ─────────────────────────────────────────────────

def _extract_with_pdfplumber(input_path: str, intermediate_dir: str) -> dict:
    import pdfplumber

    figures_dir = os.path.join(intermediate_dir, "figures")
    os.makedirs(figures_dir, exist_ok=True)

    text_blocks = []
    tables      = []

    with pdfplumber.open(input_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            # ── Tables: use line strategy (best for academic papers) ──────────
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
                    "caption": "",          # matched from text below
                    "headers": clean[0],
                    "rows":    clean[1:],
                    "page":    page_num,
                    "notes":   "",
                })

            # ── Text ──────────────────────────────────────────────────────────
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            if text.strip():
                text_blocks.append(text)

    raw_text = "\n\n".join(text_blocks)

    # Match "Table N ..." captions from text to tables by order
    _match_captions(raw_text, tables)

    # ── Images via PyMuPDF ────────────────────────────────────────────────────
    images = _extract_images(input_path, figures_dir)

    return {"raw_text": raw_text, "tables": tables, "images": images}


def _match_captions(text: str, tables: list):
    caps = re.findall(r"(Table\s+\d+[^\.\n]*(?:\.[^\.\n]*)?)", text, re.IGNORECASE)
    for i, tbl in enumerate(tables):
        if i < len(caps):
            tbl["caption"] = caps[i].strip()


def _extract_images(pdf_path: str, figures_dir: str) -> list:
    images = []
    try:
        import fitz
        doc = fitz.open(pdf_path)
        count = 0
        for page_num, page in enumerate(doc):
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    base   = doc.extract_image(xref)
                    data   = base["image"]
                    if len(data) < 5000:
                        continue
                    count += 1
                    ext    = base["ext"]
                    fpath  = os.path.join(figures_dir, f"fig_{count}.{ext}")
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
    except ImportError:
        pass   # fitz not available — skip images
    return images


# ── pypdf fallback (text only) ────────────────────────────────────────────────

def _extract_with_pypdf(input_path: str) -> dict:
    """Fallback using pypdf (from PDF skill) — text only, no tables."""
    from pypdf import PdfReader
    reader   = PdfReader(input_path)
    parts    = []
    for page in reader.pages:
        t = page.extract_text()
        if t and t.strip():
            parts.append(t)
    return {"raw_text": "\n\n".join(parts), "tables": [], "images": []}


# ── Write outputs ─────────────────────────────────────────────────────────────

def _write_outputs(result: dict, intermediate_dir: str):
    os.makedirs(intermediate_dir, exist_ok=True)
    txt_path  = os.path.join(intermediate_dir, "extracted.txt")
    json_path = os.path.join(intermediate_dir, "extracted_rich.json")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(result["raw_text"])

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)