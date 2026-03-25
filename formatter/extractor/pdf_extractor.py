"""
extractor/pdf_extractor.py — PDF text + table + figure + formula extraction.

PyMuPDF (fitz) is PRIMARY — preserves font info, extracts images, detects formula regions.
pdfplumber is FALLBACK — text + tables only, no images or formula OCR.

Formula OCR chain (fitz path only):
    pix2tex_worker.py → nougat_worker.py  (both run as subprocesses)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import logging

log = logging.getLogger("paper_formatter")

# ── Library availability ────────────────────────────────────────
_HAS_FITZ = False
_HAS_PDFPLUMBER = False

try:
    import fitz  # PyMuPDF
    _HAS_FITZ = True
except ImportError:
    pass

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    pass

# ── Math detection thresholds (conservative — false positives waste ~10s each) ──
MIN_MATH_CHARS = 5        # min math-Unicode chars in block
MIN_MATH_RATIO = 0.08     # math chars must be >= 8% of block text
MAX_BLOCK_CHARS = 300     # skip long body paragraphs
MIN_CROP_W = 60           # minimum rendered crop width (pixels)
MIN_CROP_H = 20           # minimum rendered crop height (pixels)
MAX_PER_PAGE = 8          # cap per page
MAX_TOTAL = 40            # cap total

# TeX-specific math font substrings (conservative — avoids false STIX hits)
MATH_FONT_HINTS = {"cmex", "cmsy", "cmmi", "euler", "mathit", "mathsy"}

# Unicode math characters
MATH_CHARS = set()
for _c in range(0x0391, 0x03C9 + 1): MATH_CHARS.add(chr(_c))   # Greek
for _c in range(0x2200, 0x22FF + 1): MATH_CHARS.add(chr(_c))   # Math operators
for _c in range(0x2190, 0x21FF + 1): MATH_CHARS.add(chr(_c))   # Arrows
for _c in range(0x2070, 0x209F + 1): MATH_CHARS.add(chr(_c))   # Super/subscript digits
MATH_CHARS.update("±×÷∞≈≠≤≥∈∉⊂⊃∪∩∧∨¬∀∃∅∇∂√∫∑∏")

# Equation number pattern "(1)", "(2.1)" — skip these, not real formula blocks
EQ_NUM_RE = re.compile(r"^\s*\(\d+(?:\.\d+)?\)\s*$")


def extract_pdf(path: str) -> dict:
    """
    Extract text, blocks, tables, figures, and formula_blocks from a PDF.
    Returns dict: {text, blocks, tables, figures, formula_blocks}
    """
    log.info("  Opening PDF: %s", os.path.basename(path))
    if _HAS_FITZ:
        return _extract_with_fitz(path)
    elif _HAS_PDFPLUMBER:
        log.warning("  PyMuPDF not available — using pdfplumber (no formula/image extraction)")
        return _extract_with_pdfplumber(path)
    else:
        raise RuntimeError(
            "No PDF library available.\n"
            "Install PyMuPDF: pip install pymupdf\n"
            "  or pdfplumber: pip install pdfplumber"
        )


# ══════════════════════════════════════════════════════════════════
#  PyMuPDF (fitz) path — PRIMARY
# ══════════════════════════════════════════════════════════════════

def _extract_with_fitz(path: str) -> dict:
    """
    Full extraction using PyMuPDF:
      - Text with font name + size per block (enables font-aware parsing)
      - Images: saved as PNGs to intermediate/figures/
      - Equation images: small images routed to pix2tex OCR → FormulaBlock
      - Formula regions: detected via math-char analysis, OCR'd with pix2tex
      - Tables: via pdfplumber (text blocks overlapping tables are excluded)
    """
    import fitz

    # Output folder for extracted figures
    fig_dir = _ensure_fig_dir()

    # ── Pre-extract table bounding boxes via pdfplumber ─────────
    # Used to filter text blocks that overlap with tables (prevents duplication)
    table_bboxes_by_page = _get_table_bboxes(path)

    pdf = fitz.open(path)
    all_text = []
    all_blocks = []
    all_figures = []
    all_formulas = []
    total_formulas = 0
    fig_counter = 0

    total_pages = len(pdf)

    for page_num in range(total_pages):
        page = pdf[page_num]
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        page_text_parts = []
        page_table_bboxes = table_bboxes_by_page.get(page_num, [])

        # ── Extract text blocks (skip image blocks and table overlaps) ──
        for block in text_dict.get("blocks", []):
            btype = block.get("type", 0)
            if btype != 0:
                continue  # skip image blocks — handled by get_images() below

            block_bbox = block.get("bbox", [0, 0, 0, 0])

            # Skip text blocks that overlap with a table region
            if _bbox_overlaps_any(block_bbox, page_table_bboxes):
                log.debug("  Skipping text block overlapping table on p%d: %.0f,%.0f,%.0f,%.0f",
                          page_num, *block_bbox)
                continue

            block_text = ""
            block_fonts = []
            block_sizes = []

            for line in block.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                    block_fonts.append(span.get("font", ""))
                    block_sizes.append(span.get("size", 0))
                block_text += line_text + "\n"

            block_text = block_text.strip()
            if not block_text:
                continue

            font = max(set(block_fonts), key=block_fonts.count) if block_fonts else ""
            size = max(set(block_sizes), key=block_sizes.count) if block_sizes else 0

            all_blocks.append({
                "text": block_text,
                "font": font,
                "size": round(size, 1),
                "page": page_num,
                "bbox": block_bbox,
            })
            page_text_parts.append(block_text)

        all_text.append("\n".join(page_text_parts))

        # ── Extract ALL images via get_images() ──────────────────
        # Classifies each as equation or figure, tries OCR, falls back to image
        page_figs, page_eqs = _extract_all_page_images(
            pdf, page, page_num, fig_dir, total_pages
        )
        all_figures.extend(page_figs)
        all_formulas.extend(page_eqs)

        # ── Formula detection on text blocks (math-char analysis) ─
        # This catches formulas rendered as text with Unicode math symbols
        if len(all_formulas) < MAX_TOTAL:
            page_fbs = _detect_formula_regions(page, page_num, text_dict)
            for fb in page_fbs[:MAX_PER_PAGE]:
                if len(all_formulas) >= MAX_TOTAL:
                    break
                all_formulas.append(fb)

    fig_counter = len(all_figures)

    pdf.close()

    # Tables via pdfplumber
    all_tables = _extract_tables_pdfplumber(path)

    # Detect figure captions from nearby text blocks
    _detect_figure_captions(all_figures, all_blocks)

    full_text = "\n\n".join(all_text)
    log.info(
        "  Extracted %d chars, %d blocks, %d tables, %d figures, %d formulas (fitz)",
        len(full_text), len(all_blocks), len(all_tables),
        len(all_figures), len(all_formulas)
    )

    return {
        "text": full_text,
        "blocks": all_blocks,
        "tables": all_tables,
        "figures": all_figures,
        "formula_blocks": all_formulas,
    }


# ── Figure caption detection ──────────────────────────────────────

# Caption patterns: "Fig. 1. Caption text", "Figure 1: Caption text", etc.
_CAPTION_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?\s*\d+[\.:]\s*)(.+)",
    re.IGNORECASE | re.DOTALL
)
_CAPTION_START_RE = re.compile(
    r"^Fig(?:ure)?\.?\s*\d+",
    re.IGNORECASE
)


def _detect_figure_captions(figures: list, blocks: list):
    """
    Match extracted figures with caption text from nearby text blocks.

    Strategy: For each figure, find text blocks on the same page that start
    with "Fig." or "Figure" patterns. Pick the closest one by y-position
    (captions are typically just below or above the figure).
    """
    if not figures or not blocks:
        return

    # Build page → caption-blocks index
    caption_blocks = {}   # page_num → [(block, y_pos)]
    for b in blocks:
        text = b["text"].strip()
        if _CAPTION_START_RE.match(text):
            page = b.get("page", -1)
            bbox = b.get("bbox", [0, 0, 0, 0])
            y_pos = bbox[1]   # top y of caption block
            if page not in caption_blocks:
                caption_blocks[page] = []
            caption_blocks[page].append((b, y_pos))

    if not caption_blocks:
        return

    # For each figure, find the best matching caption on the same page
    used_captions = set()  # track to avoid double-matching

    for fig in figures:
        # Figure label encodes page: "fig_3_0" → page 3
        label = fig.get("label", "")
        parts = label.split("_")
        if len(parts) >= 2:
            try:
                fig_page = int(parts[1])
            except ValueError:
                continue
        else:
            continue

        if fig_page not in caption_blocks:
            continue

        # Find closest unused caption on this page
        best_caption = None
        best_dist = float("inf")
        best_idx = -1

        for idx, (cb, cy) in enumerate(caption_blocks[fig_page]):
            if id(cb) in used_captions:
                continue
            dist = abs(cy - 0)  # simple: prefer first available on page
            if dist < best_dist or best_caption is None:
                best_caption = cb
                best_dist = dist
                best_idx = idx

        if best_caption:
            caption_text = best_caption["text"].strip()
            # Extract caption after "Fig. N." prefix
            m = _CAPTION_RE.match(caption_text)
            if m:
                fig["caption"] = m.group(1).strip()
            else:
                fig["caption"] = caption_text
            used_captions.add(id(best_caption))
            log.debug("  Caption matched: %s → '%s'",
                      fig.get("label", ""), fig["caption"][:60])


# ── Table bbox overlap detection ─────────────────────────────────

def _get_table_bboxes(path: str) -> dict:
    """
    Get table bounding boxes per page using pdfplumber.
    Returns {page_num: [bbox_tuple, ...]} where bbox = (x0, y0, x1, y1).
    """
    if not _HAS_PDFPLUMBER:
        return {}

    result = {}
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                try:
                    tables = page.find_tables()
                    if tables:
                        result[page_num] = [t.bbox for t in tables]
                except Exception as e:
                    log.debug("  Table bbox detection failed p%d: %s", page_num, e)
    except Exception as e:
        log.debug("  pdfplumber open failed for table bboxes: %s", e)

    return result


def _bbox_overlaps_any(block_bbox, table_bboxes, threshold=0.5) -> bool:
    """
    Check if a text block overlaps significantly with any table bbox.
    Returns True if >= threshold of the block area is inside a table.
    Both bboxes are (x0, y0, x1, y1).
    """
    if not table_bboxes:
        return False

    bx0, by0, bx1, by1 = block_bbox
    block_area = max((bx1 - bx0) * (by1 - by0), 1)

    for tx0, ty0, tx1, ty1 in table_bboxes:
        # Intersection
        ix0 = max(bx0, tx0)
        iy0 = max(by0, ty0)
        ix1 = min(bx1, tx1)
        iy1 = min(by1, ty1)

        if ix0 < ix1 and iy0 < iy1:
            overlap_area = (ix1 - ix0) * (iy1 - iy0)
            if overlap_area / block_area >= threshold:
                return True

    return False


# ── Image classification ──────────────────────────────────────────

# Size thresholds for classification (in PDF points, 72pt = 1 inch)
MIN_IMG_SIZE = 15        # skip tiny images (icons, bullets)
EQ_MAX_HEIGHT = 120      # equations are typically short
FIG_MIN_HEIGHT = 100     # figures are tall
FIG_MIN_WIDTH = 150      # figures are wide
FIG_MIN_AREA = 15000     # figures have significant area


def _classify_image(w: float, h: float, page_num: int, total_pages: int) -> str:
    """
    Classify an image as 'equation', 'figure', or 'skip'.
    Uses size, aspect ratio, and page position heuristics.
    """
    # Too small — icon, bullet, decorative
    if w < MIN_IMG_SIZE or h < MIN_IMG_SIZE:
        return "skip"

    # Very small area — likely a logo or symbol
    area = w * h
    if area < 500:
        return "skip"

    # Large images with significant height → figure
    if h >= FIG_MIN_HEIGHT and w >= FIG_MIN_WIDTH and area >= FIG_MIN_AREA:
        # But matrices can be large too — check aspect ratio
        ratio = w / max(h, 1)
        if ratio < 3.0:  # not extremely wide → likely a figure
            return "figure"

    # Medium to small images → equation
    if h < EQ_MAX_HEIGHT:
        return "equation"

    # Tall but narrow → could be a matrix equation
    if w < 300:
        return "equation"

    # Default: treat as figure
    return "figure"


# ── Image-based equation extraction (OCR-or-save) ────────────────

def _process_equation_image(pdf, img_bytes: bytes, ext: str,
                             page_num: int, counter: int, fig_dir: str,
                             bbox_y: float = 0.0) -> dict:
    """
    Process a small image as a math equation.
    Strategy: try OCR (pix2tex → nougat), fall back to saving the image directly.
    Returns a formula dict with either 'latex' or 'image_path' set.
    """
    if not img_bytes or len(img_bytes) < 100:
        return None

    # Save image to figures dir (needed for both OCR and fallback)
    fname = f"eq_{page_num}_{counter}.png"
    fpath = os.path.join(fig_dir, fname)

    # For OCR, we need a temp file (or we can use the saved file)
    with open(fpath, "wb") as f:
        f.write(img_bytes)

    # Try OCR: pix2tex → nougat
    latex = _run_ocr_worker("pix2tex", fpath)
    if not latex:
        latex = _run_ocr_worker("nougat", fpath)

    if latex:
        latex = _sanitize_ocr_latex(latex)

    if latex:
        confidence = _score_ocr_quality(latex)
        log.debug("  Equation OCR p%d: conf=%.2f latex=%s", page_num, confidence, latex[:60])
        if confidence >= 0.45:
            return {
                "latex": latex,
                "image_path": fpath,  # keep image as backup
                "confidence": confidence,
                "page": page_num,
                "label": f"eq_{page_num}_{counter}",
                "bbox_y": bbox_y,
            }

    # OCR failed or unavailable → save image as-is for \includegraphics fallback
    log.debug("  Equation image saved (no OCR) p%d: %s", page_num, fname)
    return {
        "latex": "",
        "image_path": fpath,
        "confidence": 0.5,   # medium confidence — it IS an equation image
        "page": page_num,
        "label": f"eq_{page_num}_{counter}",
        "bbox_y": bbox_y,
    }


# ── Primary image extraction via get_images() ────────────────────

def _extract_all_page_images(pdf, page, page_num: int, fig_dir: str,
                              total_pages: int) -> tuple:
    """
    Extract ALL images from a page.
    Uses get_images(full=True) for image bytes, and text dict blocks for
    rendered dimensions (PDF points) — critical for correct classification.
    Returns (figures_list, formulas_list).
    """
    import fitz

    figures = []
    formulas = []

    # Phase 1: Build xref → rendered bbox mapping from text dict.
    # text dict type=1 blocks have bbox in PDF points (the RENDERED size).
    # pdf.extract_image() returns PIXEL dimensions (much larger) — don't use for classification.
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    xref_to_rendered = {}   # xref → (w_pts, h_pts, bbox_y)
    inline_blocks = []      # type=1 blocks without xref (inline images)

    for block in text_dict.get("blocks", []):
        if block.get("type") != 1:
            continue
        xref = block.get("xref", 0)
        bbox = block.get("bbox", [0, 0, 0, 0])
        w_pts = bbox[2] - bbox[0]
        h_pts = bbox[3] - bbox[1]
        if xref > 0:
            xref_to_rendered[xref] = (w_pts, h_pts, bbox[1], bbox)
        else:
            inline_blocks.append((block, w_pts, h_pts, bbox[1]))

    # Phase 2: Process images via get_images() (has xrefs for byte extraction)
    seen_xrefs = set()

    try:
        images = page.get_images(full=True)
    except Exception as e:
        log.debug("  get_images() failed p%d: %s", page_num, e)
        images = []

    for img_info in images:
        xref = img_info[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)

        try:
            img_data = pdf.extract_image(xref)
            if not img_data or not img_data.get("image"):
                continue

            img_bytes = img_data["image"]
            ext = img_data.get("ext", "png")

            if len(img_bytes) < 100:
                continue

            # Use RENDERED dimensions (PDF points) for classification, not pixels
            if xref in xref_to_rendered:
                w, h, bbox_y, img_bbox = xref_to_rendered[xref]
            else:
                # Fallback: estimate from pixel dims (assume ~150 DPI)
                pw = img_data.get("width", 0)
                ph = img_data.get("height", 0)
                w = pw * 72.0 / 150.0   # convert pixels → approx PDF points
                h = ph * 72.0 / 150.0
                bbox_y = 0.0

            classification = _classify_image(w, h, page_num, total_pages)

            if classification == "skip":
                log.debug("  Skip image p%d xref=%d (%.0fx%.0f pt)", page_num, xref, w, h)
                continue

            elif classification == "equation":
                # Prefer pixmap rendering (avoids black-box from exotic image formats)
                eq_bytes = img_bytes
                eq_ext = ext
                if xref in xref_to_rendered:
                    try:
                        mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(img_bbox))
                        eq_bytes = pix.tobytes("png")
                        eq_ext = "png"
                    except Exception as e:
                        log.debug("  Pixmap render failed p%d xref=%d: %s", page_num, xref, e)
                fb = _process_equation_image(
                    pdf, eq_bytes, eq_ext, page_num, len(formulas),
                    fig_dir, bbox_y=bbox_y
                )
                if fb:
                    formulas.append(fb)
                    log.debug("  Equation p%d xref=%d (%.0fx%.0f pt) → %s",
                              page_num, xref, w, h,
                              "OCR" if fb.get("latex") else "image")

            elif classification == "figure":
                # Prefer pixmap rendering for figures too (avoids black boxes
                # from exotic image formats like JBIG2, SMask, CMYK)
                fig_bytes = img_bytes
                fig_ext = ext
                if xref in xref_to_rendered:
                    try:
                        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI for figures
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(img_bbox))
                        fig_bytes = pix.tobytes("png")
                        fig_ext = "png"
                    except Exception as e:
                        log.debug("  Figure pixmap render failed p%d xref=%d: %s",
                                  page_num, xref, e)
                fname = f"fig_{page_num}_{len(figures)}.{fig_ext}"
                fpath = os.path.join(fig_dir, fname)
                with open(fpath, "wb") as f:
                    f.write(fig_bytes)
                figures.append({
                    "image_path": fpath,
                    "caption": "",
                    "label": f"fig_{page_num}_{len(figures)}",
                })
                log.debug("  Figure p%d xref=%d (%.0fx%.0f pt): %s",
                          page_num, xref, w, h, fname)

        except Exception as e:
            log.debug("  Image extraction failed p%d xref=%d: %s", page_num, xref, e)

    # Phase 3: Handle inline images (no xref — render from bbox)
    for block, w, h, bbox_y in inline_blocks:
        bbox = block.get("bbox")
        if not bbox or w < MIN_IMG_SIZE or h < MIN_IMG_SIZE:
            continue

        classification = _classify_image(w, h, page_num, total_pages)
        if classification == "skip":
            continue

        try:
            mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
            pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox))
            img_bytes = pix.tobytes("png")

            if classification == "equation":
                fb = _process_equation_image(
                    pdf, img_bytes, "png", page_num, len(formulas),
                    fig_dir, bbox_y=bbox_y
                )
                if fb:
                    formulas.append(fb)
            elif classification == "figure":
                fname = f"fig_{page_num}_{len(figures)}.png"
                fpath = os.path.join(fig_dir, fname)
                with open(fpath, "wb") as f:
                    f.write(img_bytes)
                figures.append({
                    "image_path": fpath,
                    "caption": "",
                    "label": f"fig_{page_num}_{len(figures)}",
                })
        except Exception as e:
            log.debug("  Inline image failed p%d: %s", page_num, e)

    return figures, formulas


# ── Image extraction ─────────────────────────────────────────────

def _ensure_fig_dir() -> str:
    """Create intermediate/figures/ directory and return its path."""
    from core.config import INTERMEDIATE_DIR
    fig_dir = os.path.join(INTERMEDIATE_DIR, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    return fig_dir


def _extract_image_block(pdf, block: dict, fig_dir: str, counter: int, page_num: int) -> dict:
    """
    Save an image block as PNG to fig_dir.
    Returns figure dict {image_path, caption, label} or None.
    """
    try:
        import fitz

        # fitz image block has an "image" key with raw bytes, or xref
        xref = block.get("xref", 0)
        if xref > 0:
            img_data = pdf.extract_image(xref)
            if not img_data:
                return None
            ext = img_data.get("ext", "png")
            img_bytes = img_data.get("image", b"")
        else:
            # Inline image — render the bbox region
            bbox = block.get("bbox")
            if not bbox:
                return None
            page = pdf[page_num]
            mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
            pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox))
            img_bytes = pix.tobytes("png")
            ext = "png"

        if not img_bytes or len(img_bytes) < 200:
            return None  # skip truly empty images (lowered from 500)

        fname = f"fig_{page_num}_{counter}.{ext}"
        fpath = os.path.join(fig_dir, fname)
        with open(fpath, "wb") as f:
            f.write(img_bytes)

        return {
            "image_path": fpath,
            "caption": "",
            "label": f"fig_{page_num}_{counter}",
        }

    except Exception as e:
        log.debug("  Image block extraction failed (p%d): %s", page_num, e)
        return None


# ── Formula region detection ─────────────────────────────────────

def _detect_formula_regions(page, page_num: int, text_dict: dict) -> list:
    """
    Detect formula regions on a page using math-char analysis.
    Renders each candidate as PNG and OCR's with pix2tex.
    Returns list of dicts: {latex, confidence, page, label}
    """
    formulas = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue

        block_text = ""
        fonts = set()
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                block_text += span.get("text", "")
                fonts.add(span.get("font", "").lower())

        block_text = block_text.strip()
        if not block_text or len(block_text) > MAX_BLOCK_CHARS:
            continue

        # Skip standalone equation numbers like "(1)"
        if EQ_NUM_RE.match(block_text):
            continue

        # Count math characters
        math_count = sum(1 for ch in block_text if ch in MATH_CHARS)

        is_formula = False

        # Method 1: math char count + ratio
        if math_count >= MIN_MATH_CHARS:
            ratio = math_count / max(len(block_text), 1)
            if ratio >= MIN_MATH_RATIO:
                is_formula = True

        # Method 2: TeX math font detection (needs >=2 math chars too)
        if not is_formula and math_count >= 2:
            for f in fonts:
                if any(hint in f for hint in MATH_FONT_HINTS):
                    is_formula = True
                    break

        if not is_formula:
            continue

        # Check minimum crop size
        bbox = block.get("bbox")
        if not bbox:
            continue
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w < MIN_CROP_W or h < MIN_CROP_H:
            continue

        # OCR the region
        latex = _ocr_formula_region(page, bbox)
        if not latex:
            continue

        confidence = _score_ocr_quality(latex)
        log.debug("  Formula p%d: conf=%.2f latex=%s", page_num, confidence, latex[:60])

        formulas.append({
            "latex": latex,
            "confidence": confidence,
            "page": page_num,
            "label": f"eq_{page_num}_{len(formulas)+1}",
        })

    return formulas


def _ocr_formula_region(page, bbox) -> str:
    """Render a page region at 200 DPI and OCR with pix2tex → nougat fallback."""
    try:
        import fitz
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            pix.save(f.name)
            tmp_path = f.name

        latex = _run_ocr_worker("pix2tex", tmp_path)
        if not latex:
            latex = _run_ocr_worker("nougat", tmp_path)

        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        if latex:
            latex = _sanitize_ocr_latex(latex)

        return latex or ""

    except Exception as e:
        log.debug("  OCR region failed: %s", e)
        return ""


# ── OCR subprocess runner ────────────────────────────────────────

def _get_python_exe() -> str:
    """
    Find the Python executable to use for subprocess workers.
    Prefers the venv Python so pix2tex / nougat are available.
    Falls back to sys.executable.
    """
    # Check if we're inside a venv already
    if hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    ):
        return sys.executable  # already in venv — use current Python

    # Look for venv relative to project root (formatter/venv/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in [
        os.path.join(project_root, "venv", "Scripts", "python.exe"),  # Windows
        os.path.join(project_root, "venv", "bin", "python"),           # Linux/Mac
        os.path.join(project_root, ".venv", "Scripts", "python.exe"),
        os.path.join(project_root, ".venv", "bin", "python"),
    ]:
        if os.path.isfile(candidate):
            log.debug("  OCR worker will use venv Python: %s", candidate)
            return candidate

    return sys.executable  # fallback


_PYTHON_EXE = None  # cached

# ── OCR availability caching ──────────────────────────────────────
# Check once at first use, then skip all calls if unavailable.
# This prevents 30s timeouts per equation when pix2tex/nougat aren't installed.
_OCR_AVAIL = {}   # engine → True/False, cached per process

def _check_ocr_available(engine: str) -> bool:
    """Check if an OCR engine is installed and importable. Cached per process."""
    global _PYTHON_EXE, _OCR_AVAIL
    if engine in _OCR_AVAIL:
        return _OCR_AVAIL[engine]

    if _PYTHON_EXE is None:
        _PYTHON_EXE = _get_python_exe()

    # Quick import check (5s timeout — just tests if the module exists)
    try:
        if engine == "pix2tex":
            check_cmd = [_PYTHON_EXE, "-c", "import pix2tex; print('ok')"]
        elif engine == "nougat":
            check_cmd = [_PYTHON_EXE, "-c", "from nougat.utils.checkpoint import get_checkpoint; print('ok')"]
        else:
            _OCR_AVAIL[engine] = False
            return False

        result = subprocess.run(check_cmd, capture_output=True, timeout=10)
        available = result.returncode == 0 and "ok" in result.stdout.decode("utf-8", errors="replace")
        _OCR_AVAIL[engine] = available
        if available:
            log.info("  OCR engine '%s' is available", engine)
        else:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            log.info("  OCR engine '%s' not available: %s", engine, stderr[:120])
    except subprocess.TimeoutExpired:
        log.info("  OCR engine '%s' availability check timed out", engine)
        _OCR_AVAIL[engine] = False
    except Exception as e:
        log.info("  OCR engine '%s' availability check failed: %s", engine, e)
        _OCR_AVAIL[engine] = False

    return _OCR_AVAIL.get(engine, False)


def _run_ocr_worker(engine: str, image_path: str) -> str:
    """Run pix2tex or nougat worker in a subprocess. Returns LaTeX or ''."""
    # Fast path: skip if engine is known to be unavailable
    if not _check_ocr_available(engine):
        return ""

    global _PYTHON_EXE
    if _PYTHON_EXE is None:
        _PYTHON_EXE = _get_python_exe()

    worker_dir = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(worker_dir, f"{engine}_worker.py")

    if not os.path.isfile(worker):
        return ""

    try:
        result = subprocess.run(
            [_PYTHON_EXE, worker, image_path],
            capture_output=True,
            timeout=60,  # increased from 30 — pix2tex first run downloads models
        )
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()

        if result.returncode == 0 and stdout:
            return stdout

        if stderr:
            log.debug("  %s worker stderr: %s", engine, stderr[:200])

    except subprocess.TimeoutExpired:
        log.debug("  %s worker timed out", engine)
        # Mark as unavailable to prevent further timeouts
        _OCR_AVAIL[engine] = False
    except Exception as e:
        log.debug("  %s worker failed: %s", engine, e)

    return ""


# ── OCR LaTeX sanitization ───────────────────────────────────────

def _sanitize_ocr_latex(latex: str) -> str:
    """4-step sanitization chain. Returns '' if result would crash pdflatex."""
    latex = _fix_array_col_spec(latex)
    latex = _fix_unbalanced_braces(latex)
    latex = _fix_unbalanced_delimiters(latex)
    if not _is_latex_safe(latex):
        log.debug("  OCR rejected by safety gate: %s", latex[:80])
        return ""
    return latex


def _fix_array_col_spec(latex: str) -> str:
    """Fix \\begin{array}{col} when declared cols != actual & count."""
    pat = re.compile(r"\\begin\{array\}\{([^}]*)\}")
    m = pat.search(latex)
    if not m:
        return latex
    max_amps = max((row.count("&") for row in latex.split(r"\\")), default=0)
    new_spec = "c" * (max_amps + 1)
    return pat.sub(f"\\\\begin{{array}}{{{new_spec}}}", latex, count=1)


def _fix_unbalanced_braces(latex: str) -> str:
    """Depth-walk to balance { and }."""
    depth = sum(1 if c == "{" else (-1 if c == "}" else 0) for c in latex)
    if depth > 0:
        latex += "}" * depth
    elif depth < 0:
        latex = "{" * (-depth) + latex
    return latex


def _fix_unbalanced_delimiters(latex: str) -> str:
    """Balance \\left / \\right pairs."""
    lefts  = len(re.findall(r"\\left[\(\[\{|.]",  latex))
    rights = len(re.findall(r"\\right[\)\]\}|.]", latex))
    if lefts > rights:
        latex += r"\right." * (lefts - rights)
    elif rights > lefts:
        latex = r"\left."  * (rights - lefts) + latex
    return latex


def _is_latex_safe(latex: str) -> bool:
    """Final gate — reject output that will crash pdflatex."""
    if not latex or len(latex) < 2:
        return False
    # Brace balance
    depth = 0
    for c in latex:
        if c == "{": depth += 1
        elif c == "}": depth -= 1
        if depth < 0: return False
    if depth != 0:
        return False
    # begin/end balance
    if len(re.findall(r"\\begin\{", latex)) != len(re.findall(r"\\end\{", latex)):
        return False
    return True


# ── OCR quality scoring ──────────────────────────────────────────

def _score_ocr_quality(latex: str) -> float:
    """
    Heuristic quality score 0.0–1.0.
    Penalizes prose-as-math (high \\mathrm ratio, tilde-words, \\scriptstyle).
    Rewards real math structures (\\frac, ^{}, \\int, etc.)
    """
    if not latex:
        return 0.0

    score = 0.5

    # Penalize high \mathrm{...} ratio
    mathrm = re.findall(r"\\mathrm\{([^}]*)\}", latex)
    mathrm_chars = sum(len(m) for m in mathrm)
    if mathrm_chars / max(len(latex), 1) > 0.5:
        score -= 0.3
    # Penalize tilde-separated words inside \mathrm (pix2tex space encoding)
    for m in mathrm:
        if m.count("~") >= 2:
            score -= 0.2
            break
    # Penalize \scriptstyle wrapping large blocks
    if r"\scriptstyle" in latex and len(latex) > 50:
        score -= 0.15

    # Reward real math
    for pat in [r"\frac", r"\int", r"\sum", r"\prod", r"\lim",
                "^{", "_{", r"\alpha", r"\beta", r"\partial"]:
        if pat in latex:
            score += 0.05
    if re.search(r"[+\-=<>]", latex):
        score += 0.05

    return max(0.0, min(1.0, score))


# ══════════════════════════════════════════════════════════════════
#  pdfplumber path — FALLBACK (text + tables only, no images/formulas)
# ══════════════════════════════════════════════════════════════════

def _extract_with_pdfplumber(path: str) -> dict:
    """Extract using pdfplumber. No image or formula extraction."""
    import pdfplumber

    all_text = []
    all_blocks = []
    all_tables = []

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages):

            # Build blocks from character-level y-position clustering
            # This gives us proper font size AND paragraph boundaries
            page_blocks, page_text = _build_blocks_from_chars(page, page_num)

            # If char-based extraction fails, fall back to extract_text()
            if not page_blocks:
                fallback_text = page.extract_text() or ""
                all_text.append(fallback_text)
                if fallback_text.strip():
                    all_blocks.append({
                        "text": fallback_text, "font": "", "size": 0,
                        "page": page_num, "bbox": [0, 0, page.width or 612, 0],
                    })
            else:
                all_text.append(page_text)
                all_blocks.extend(page_blocks)

            # Tables
            try:
                for td in (page.extract_tables() or []):
                    if td and len(td) >= 2:
                        all_tables.append({
                            "headers": [str(c) if c else "" for c in td[0]],
                            "rows":    [[str(c) if c else "" for c in row] for row in td[1:]],
                            "caption": "",
                            "label":   f"tab_{page_num}_{len(all_tables)+1}",
                        })
            except Exception as e:
                log.debug("  pdfplumber table error p%d: %s", page_num, e)

    full_text = "\n\n".join(all_text)
    log.info("  Extracted %d chars, %d blocks, %d tables (pdfplumber)",
             len(full_text), len(all_blocks), len(all_tables))

    return {
        "text": full_text,
        "blocks": all_blocks,
        "tables": all_tables,
        "figures": [],
        "formula_blocks": [],
    }


def _build_blocks_from_chars(page, page_num: int):
    """
    Build text blocks from pdfplumber characters grouped by y-position.

    Returns (blocks_list, full_page_text).
    A new block starts when the vertical gap between lines exceeds 1.5× line height.
    This properly recovers both word spaces and paragraph boundaries.
    """
    try:
        chars = page.chars
    except Exception:
        return [], ""

    if not chars:
        return [], ""

    # Average char width for space recovery
    widths = [c.get("x1", 0) - c.get("x0", 0) for c in chars]
    widths = [w for w in widths if 0.5 < w < 30]
    if not widths:
        return [], ""
    avg_w = sum(widths) / len(widths)
    space_threshold = avg_w * 0.4

    # Average line height for paragraph gap detection
    heights = [c.get("bottom", 0) - c.get("top", 0) for c in chars
               if c.get("bottom", 0) > c.get("top", 0)]
    heights = [h for h in heights if 2 < h < 50]
    avg_line_h = sum(heights) / len(heights) if heights else 12

    # Group chars into lines by y-position (rounded to 1dp)
    lines_by_y = {}
    for c in chars:
        y = round(c.get("top", 0), 1)
        lines_by_y.setdefault(y, []).append(c)

    # Reconstruct each line with proper word spacing
    sorted_ys = sorted(lines_by_y.keys())
    line_texts = []   # (y_top, line_size, line_text)
    for y in sorted_ys:
        line_chars = sorted(lines_by_y[y], key=lambda c: c.get("x0", 0))
        line_text = ""
        prev_x1 = None
        sizes = []
        for c in line_chars:
            ch = c.get("text", "")
            if not ch:
                continue
            x0 = c.get("x0", 0)
            sz = c.get("bottom", 0) - c.get("top", 0)
            if sz > 0:
                sizes.append(sz)
            if prev_x1 is not None and (x0 - prev_x1) > space_threshold:
                line_text += " "
            line_text += ch
            prev_x1 = c.get("x1", x0 + avg_w)
        if line_text.strip():
            avg_sz = sum(sizes) / len(sizes) if sizes else avg_line_h
            line_texts.append((y, round(avg_sz, 1), line_text.strip()))

    if not line_texts:
        return [], ""

    # Group lines into blocks based on y-gap
    # Gap > 1.5× avg line height = paragraph break
    gap_threshold = avg_line_h * 1.5

    blocks = []
    current_lines = [line_texts[0]]

    for i in range(1, len(line_texts)):
        prev_y = line_texts[i - 1][0]
        curr_y = line_texts[i][0]
        gap = curr_y - prev_y

        if gap > gap_threshold:
            # Save current block
            block_text = "\n".join(t for _, _, t in current_lines)
            block_size = max(s for _, s, _ in current_lines)
            blocks.append({
                "text": block_text, "font": "",
                "size": round(block_size, 1), "page": page_num,
                "bbox": [0, current_lines[0][0], page.width or 612, prev_y],
            })
            current_lines = [line_texts[i]]
        else:
            current_lines.append(line_texts[i])

    # Last block
    if current_lines:
        block_text = "\n".join(t for _, _, t in current_lines)
        block_size = max(s for _, s, _ in current_lines)
        blocks.append({
            "text": block_text, "font": "",
            "size": round(block_size, 1), "page": page_num,
            "bbox": [0, current_lines[0][0], page.width or 612, current_lines[-1][0]],
        })

    full_text = "\n\n".join(b["text"] for b in blocks)
    return blocks, full_text


def _recover_spaces_from_chars(page) -> str:
    """
    Reconstruct text with word boundaries using char x-position gaps.
    Fixes space-stripped fonts where pdfplumber merges all chars into one word.
    Gap > 40% of average char width = word boundary.
    """
    try:
        chars = page.chars
    except Exception:
        return ""

    if not chars:
        return ""

    # Group by line (y-position, rounded to 1 decimal)
    lines_by_y = {}
    for c in chars:
        y = round(c.get("top", 0), 1)
        lines_by_y.setdefault(y, []).append(c)

    # Average char width (filter outliers)
    widths = [c.get("x1", 0) - c.get("x0", 0) for c in chars]
    widths = [w for w in widths if 0.5 < w < 30]
    if not widths:
        return ""
    avg_w = sum(widths) / len(widths)
    threshold = avg_w * 0.4

    result = []
    for y in sorted(lines_by_y):
        sorted_chars = sorted(lines_by_y[y], key=lambda c: c.get("x0", 0))
        line = ""
        prev_x1 = None
        for c in sorted_chars:
            ch = c.get("text", "")
            if not ch:
                continue
            x0 = c.get("x0", 0)
            if prev_x1 is not None and (x0 - prev_x1) > threshold:
                line += " "
            line += ch
            prev_x1 = c.get("x1", x0 + avg_w)
        if line.strip():
            result.append(line.strip())

    return "\n".join(result)


# ── Table extraction via pdfplumber (called from fitz path) ──────

def _extract_tables_pdfplumber(path: str) -> list:
    """Extract tables using pdfplumber. Returns list of dicts."""
    if not _HAS_PDFPLUMBER:
        return []
    tables = []
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                try:
                    for td in (page.extract_tables() or []):
                        if td and len(td) >= 2:
                            tables.append({
                                "headers": [str(c) if c else "" for c in td[0]],
                                "rows":    [[str(c) if c else "" for c in row] for row in td[1:]],
                                "caption": "",
                                "label":   f"tab_{page_num}_{len(tables)+1}",
                            })
                except Exception as e:
                    log.debug("  pdfplumber table error p%d: %s", page_num, e)
    except Exception as e:
        log.warning("  pdfplumber failed: %s", e)
    return tables