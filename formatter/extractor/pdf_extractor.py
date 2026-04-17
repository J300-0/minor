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
import io

from core.shared import MATH_CHARS, OCR_CONFIDENCE_THRESHOLD, TABLE_CELL_OCR_THRESHOLD

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

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ── Math detection thresholds (conservative — false positives waste ~10s each) ──
MIN_MATH_CHARS = 5        # min math-Unicode chars in block
MIN_MATH_RATIO = 0.08     # math chars must be >= 8% of block text
MAX_BLOCK_CHARS = 300     # skip long body paragraphs
MIN_CROP_W = 60           # minimum rendered crop width (pixels)
MIN_CROP_H = 20           # minimum rendered crop height (pixels)
MAX_PER_PAGE = 6          # cap per page (raised from 3 — papers can have 4+ formulas per page)
MAX_TOTAL = 20            # cap total   (raised from 10 — papers with 8+ formulas need headroom)

# Global OCR time budget — stops formula-region OCR after this many seconds.
# Formulas that didn't get OCR'd fall back to image-only rendering (still appear
# in the PDF, just as includegraphics rather than selectable LaTeX).

class _OcrBudget:
    """Encapsulated OCR time budget with proper reset semantics."""
    def __init__(self, seconds=90):
        self.max_seconds = seconds
        self.reset()

    def reset(self):
        """Reset budget for a new extraction run."""
        self.start = None
        self.exhausted = False
        self.enabled = True

    def configure(self, seconds):
        """
        Configure the OCR time budget before running extraction.
        Pass None or a large value to disable the budget.
        Pass 0 to disable OCR entirely (--no-ocr mode).
        """
        self.reset()
        if seconds is not None and seconds <= 0:
            self.enabled = False
            self.exhausted = True
        elif seconds is not None and seconds > 0:
            self.max_seconds = seconds

    def is_available(self) -> bool:
        """Check if OCR budget allows more processing."""
        if self.exhausted or not self.enabled:
            return False
        import time as _time
        if self.start is None:
            self.start = _time.time()
            return True
        if _time.time() - self.start > self.max_seconds:
            self.exhausted = True
            return False
        return True

_ocr_budget = _OcrBudget()

# Cache for pdfplumber table extraction to avoid double-opening PDFs
_pdfplumber_cache = {}  # path -> (tables_list, bboxes_dict)


def set_ocr_budget(seconds):
    """Configure the OCR time budget. Backwards-compatible wrapper."""
    _ocr_budget.configure(seconds)

# TeX-specific math font substrings (conservative — avoids false STIX hits)
MATH_FONT_HINTS = {"cmex", "cmsy", "cmmi", "euler", "mathit", "mathsy"}

# Equation number pattern "(1)", "(2.1)" — skip these, not real formula blocks
EQ_NUM_RE = re.compile(r"^\s*\(\d+(?:\.\d+)?\)\s*$")

# Extract the digit(s) from equation number text like "(7)" or "(2.1)"
_EQ_NUM_EXTRACT_RE = re.compile(r"\((\d+(?:\.\d+)?)\)")


def extract_pdf(path: str) -> dict:
    """
    Extract text, blocks, tables, figures, and formula_blocks from a PDF.
    Returns dict: {text, blocks, tables, figures, formula_blocks}
    """
    _ocr_budget.reset()  # clean state for each extraction run
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

    total_pages = len(pdf)

    # ── Pre-pass: find recurring xrefs (logos/running headers) ──
    # Any image xref appearing on 2+ pages is almost certainly a journal
    # logo, masthead, or running header — never an equation. Skip them.
    xref_page_count = {}
    for _pn in range(total_pages):
        try:
            for _img in pdf[_pn].get_images(full=True):
                _xref = _img[0]
                if _xref > 0:
                    xref_page_count[_xref] = xref_page_count.get(_xref, 0) + 1
        except Exception:
            continue
    recurring_xrefs = {x for x, n in xref_page_count.items() if n >= 2}
    if recurring_xrefs:
        log.info("  Skipping %d recurring image xref(s) (logos/headers): %s",
                 len(recurring_xrefs), sorted(recurring_xrefs))

    for page_num in range(total_pages):
        page = pdf[page_num]
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        page_text_parts = []
        page_table_bboxes = table_bboxes_by_page.get(page_num, [])

        # ── Extract ALL images FIRST so we know formula y-positions ──
        # (reordered: images before text blocks, so we can split text
        # blocks whose line-y-range crosses an equation image)
        page_figs, page_eqs = _extract_all_page_images(
            pdf, page, page_num, fig_dir, total_pages,
            table_bboxes=page_table_bboxes,
            text_dict=text_dict,
            skip_xrefs=recurring_xrefs,
        )
        all_figures.extend(page_figs)
        all_formulas.extend(page_eqs)

        # Y-positions of equations on this page — used to split text blocks
        # whose lines straddle an inline equation image. Without this, PyMuPDF
        # groups text above+below an equation into a single block → merged
        # paragraphs → formulas dumped at section end.
        page_eq_ys = sorted(
            (eq.get("bbox_y", 0), eq.get("bbox_y", 0) + eq.get("bbox_h", 0))
            for eq in page_eqs
            if eq.get("bbox_y", 0) > 0
        )

        # ── Extract text blocks (skip image blocks and table overlaps) ──
        for block in text_dict.get("blocks", []):
            btype = block.get("type", 0)
            if btype != 0:
                continue  # skip image blocks — handled by get_images() above

            block_bbox = block.get("bbox", [0, 0, 0, 0])

            # Skip text blocks that overlap with a table region.
            # Use two checks: area-based overlap (for narrow blocks) and
            # y-range containment (for wide blocks where area ratio is diluted).
            if _bbox_overlaps_any(block_bbox, page_table_bboxes, threshold=0.3):
                log.debug("  Skipping text block overlapping table on p%d: %.0f,%.0f,%.0f,%.0f",
                          page_num, *block_bbox)
                continue
            if page_table_bboxes and len(block_bbox) == 4:
                _, by0, _, by1 = block_bbox
                y_center = (by0 + by1) / 2
                in_table = any(ty0 <= y_center <= ty1
                               for _, ty0, _, ty1 in page_table_bboxes)
                if in_table:
                    log.debug("  Skipping text block inside table y-range on p%d", page_num)
                    continue

            # Split block into sub-blocks at equation y-boundaries so that
            # paragraphs around an inline formula are separated correctly.
            sub_blocks = _split_block_by_formulas(block, page_eq_ys)

            for sub in sub_blocks:
                block_text = sub["text"].strip()
                if not block_text:
                    continue
                all_blocks.append({
                    "text": block_text,
                    "font": sub["font"],
                    "size": round(sub["size"], 1),
                    "page": page_num,
                    "bbox": sub["bbox"],
                })
                page_text_parts.append(block_text)

        all_text.append("\n".join(page_text_parts))

        # ── Formula detection on text blocks (math-char analysis) ─
        # This catches formulas rendered as text with Unicode math symbols
        # Skip blocks that overlap tables (already handled by CELLIMG)
        if len(all_formulas) < MAX_TOTAL:
            page_fbs = _detect_formula_regions(page, page_num, text_dict,
                                               table_bboxes=page_table_bboxes)
            for fb in page_fbs[:MAX_PER_PAGE]:
                if len(all_formulas) >= MAX_TOTAL:
                    break
                all_formulas.append(fb)

    pdf.close()

    # ── Batch OCR: run pix2tex once for ALL equation images ──────
    # This loads the model once instead of per-equation (10x faster)
    all_formulas = _batch_ocr_equations(all_formulas)

    # ── Clear printed equation numbers from extraction ───────────
    # Printed-number matching is unreliable (false positives on logos/figures
    # classified as equations, wrong y-proximity matches). The pipeline now
    # assigns sequential numbers in reading order AFTER section distribution.
    for _f in all_formulas:
        _f["equation_number"] = ""

    # Tables via pdfplumber
    all_tables = _extract_tables_pdfplumber(path)

    # OCR table cell images → convert \CELLIMG{} to \CELLEQ{latex} for selectability
    all_tables = _ocr_table_cells(all_tables)

    # Validate all saved PNGs — remove corrupt images that would crash pdflatex
    all_formulas = _validate_formula_images(all_formulas)
    all_figures = _validate_figure_images(all_figures)
    all_tables = _validate_table_cell_images(all_tables)

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

# Caption patterns: "Fig. 1. Caption text", "Figure 1: Caption text",
# "Fig 6 Caption text" (no period/colon after number), etc.
_CAPTION_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?\s*\d+[\s\.:]+)(.+)",
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

        # Get figure's y-position for proximity matching
        fig_y = fig.get("bbox_y", 0) if isinstance(fig, dict) else getattr(fig, "bbox_y", 0)

        # Find closest unused caption on this page
        best_caption = None
        best_dist = float("inf")
        best_idx = -1

        for idx, (cb, cy) in enumerate(caption_blocks[fig_page]):
            if id(cb) in used_captions:
                continue
            dist = abs(cy - fig_y)
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

    Checks the pdfplumber cache first to avoid double-opening the PDF.
    The cache is populated by _extract_tables_pdfplumber when it runs.
    """
    # Check cache first — avoid second pdfplumber open if tables already extracted
    if path in _pdfplumber_cache:
        _, bboxes = _pdfplumber_cache[path]
        return bboxes

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


def _find_figure_caption_near(text_dict: dict, bbox: list, max_dist: float = 80.0) -> bool:
    """
    Return True if a 'Figure N' / 'Fig. N' caption text block exists within
    `max_dist` points above or below the given image bbox. Captions force
    classification to 'figure' — prevents figures (like satellite diagrams)
    from being misclassified as equations.
    """
    if not bbox or len(bbox) != 4 or not text_dict:
        return False
    import re as _re
    _cap_re = _re.compile(r"^\s*(figure|fig\.?)\s*\d", _re.IGNORECASE)
    img_y_top, img_y_bot = bbox[1], bbox[3]
    for block in text_dict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        btxt = ""
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                btxt += span.get("text", "")
            btxt += " "
        btxt = btxt.strip()
        if not _cap_re.match(btxt):
            continue
        bb = block.get("bbox", [0, 0, 0, 0])
        if len(bb) != 4:
            continue
        # Caption is within max_dist vertically (above or below the image)
        dist_above = abs(bb[3] - img_y_top)
        dist_below = abs(bb[1] - img_y_bot)
        if min(dist_above, dist_below) <= max_dist:
            return True
    return False


def _classify_image(w: float, h: float, page_num: int, total_pages: int,
                    bbox_y: float = -1.0) -> str:
    """
    Classify an image as 'equation', 'figure', or 'skip'.
    Uses size, aspect ratio, and page position heuristics.

    bbox_y: y-coordinate of the image's top edge on the page (PDF points from
            top). Used to detect mastheads/running headers on page 0.
            Pass -1 (default) when position is unknown.
    """
    # Too small — icon, bullet, decorative
    if w < MIN_IMG_SIZE or h < MIN_IMG_SIZE:
        return "skip"

    # Very small area — likely a logo or symbol
    area = w * h
    if area < 500:
        return "skip"

    # First page: skip journal mastheads in the TOP 120pt of the page.
    # Acta Avionica, IEEE, Springer, etc. all put their logo/nameplate there.
    # A real equation that high on page 0 would be unusual — the title/abstract
    # always sit above the first numbered equation.
    if page_num == 0 and 0 <= bbox_y < 120:
        # Mastheads are typically modest-sized; be conservative so we don't
        # accidentally drop a large page-0 figure that happens to sit high.
        if area < 20000:
            return "skip"

    # Last page small images are usually logos (CC, publisher, etc.) — skip
    # CC badges are wide and short; publisher logos can be medium-sized
    if page_num >= total_pages - 1:
        ratio = w / max(h, 1)
        if area < 20000 and h < 120:
            return "skip"
        if ratio > 1.8 and h < 80:  # wide and short — banner/badge/license
            return "skip"

    # Short equations (single-line): height < 120pt, any width
    # Display equations can span the full column width (400+ pt)
    if h < EQ_MAX_HEIGHT:
        return "equation"

    # Matrix equations: taller than single-line but still equations.
    # Matrices can be 120-300pt tall. Key distinction from figures:
    # - Matrices are rarely wider than 450pt (column width)
    # - Matrices have moderate aspect ratio (not extremely wide or square)
    # - Figures tend to be larger overall (area > 50000) and taller
    if h < 300:
        # Under 300pt tall — could be a large matrix or a small figure.
        # Use aspect ratio: matrices are typically wider than tall (ratio > 0.8)
        # but not extremely so. Figures tend to be more square or taller.
        ratio = w / max(h, 1)
        if w < 450:
            return "equation"  # narrow enough to be a matrix
        if ratio > 2.0:
            return "equation"  # wide and short — equation, not figure

    # Large images with significant height → figure
    if h >= FIG_MIN_HEIGHT and w >= FIG_MIN_WIDTH and area >= FIG_MIN_AREA:
        return "figure"

    # Default: treat as equation (prefer equation over figure for ambiguous cases)
    return "equation"


# ── Alpha compositing helper (fixes SMask black backgrounds) ──────

def _composite_on_white(img_bytes: bytes) -> bytes:
    """
    Composite an image with alpha/transparency onto a white background.
    Many PDF equation images use SMask (alpha mask). When extracted raw,
    transparent areas become black. This composites onto white → clean image.
    Returns PNG bytes. Falls back to original bytes on error.
    """
    try:
        img = Image.open(io.BytesIO(img_bytes))

        # If RGBA or LA or P with transparency → composite on white
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode == "LA":
                img = img.convert("RGBA")

            # Create white background
            white = Image.new("RGBA", img.size, (255, 255, 255, 255))
            composited = Image.alpha_composite(white, img)
            result = composited.convert("RGB")

            buf = io.BytesIO()
            result.save(buf, format="PNG")
            return buf.getvalue()

        # Already opaque — convert to RGB PNG to normalize format
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    except Exception as e:
        log.debug("  Alpha composite failed: %s", e)
        return img_bytes  # return original on error


def _composite_with_smask(pdf, xref: int, smask_xref: int, raw_bytes: bytes) -> bytes:
    """
    Composite an image with its separate SMask onto white background.

    PDF images can have a separate SMask (soft mask / alpha channel) stored as
    a different xref. fitz.extract_image(xref) returns ONLY the RGB data — the
    alpha mask is in smask_xref. Without compositing, transparent areas = black.

    Uses fitz.Pixmap to reconstruct the full RGBA image, then composites onto white.
    """
    try:
        import fitz

        # Method 1: Use fitz Pixmap reconstruction (most reliable)
        pix_main = fitz.Pixmap(pdf, xref)
        pix_mask = fitz.Pixmap(pdf, smask_xref)

        # Ensure mask dimensions match main image
        if pix_mask.width != pix_main.width or pix_mask.height != pix_main.height:
            # Resize mask to match (rare but possible)
            pix_mask = fitz.Pixmap(pix_mask, pix_main.width, pix_main.height, None)

        # Create RGBA pixmap by combining main image + mask as alpha channel
        pix_rgba = fitz.Pixmap(pix_main)  # copy main
        if pix_main.alpha == 0:
            pix_rgba = fitz.Pixmap(pix_main, 1)  # add alpha channel

        # Set alpha from mask
        pix_rgba.set_alpha(pix_mask.samples)

        # Composite onto white background by creating non-alpha pixmap
        # fitz.Pixmap(colorspace, src_pixmap) drops alpha compositing on white
        if pix_rgba.alpha:
            # Use PIL for reliable compositing (fitz drops alpha = black bg)
            img_data = pix_rgba.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            if img.mode == "RGBA":
                white = Image.new("RGBA", img.size, (255, 255, 255, 255))
                composited = Image.alpha_composite(white, img)
                result = composited.convert("RGB")
            else:
                result = img.convert("RGB")

            buf = io.BytesIO()
            result.save(buf, format="PNG")
            log.debug("  SMask composite: xref=%d, smask=%d → clean PNG (%dx%d)",
                      xref, smask_xref, result.width, result.height)
            return buf.getvalue()

    except Exception as e:
        log.debug("  SMask composite failed (xref=%d, smask=%d): %s", xref, smask_xref, e)

    # Method 2: Try PIL-based reconstruction from raw bytes
    try:
        # Raw bytes might still be a valid image format (JPEG, PNG)
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode in ("RGBA", "LA", "PA"):
            white = Image.new("RGBA", img.size, (255, 255, 255, 255))
            composited = Image.alpha_composite(white, img.convert("RGBA"))
            result = composited.convert("RGB")
            buf = io.BytesIO()
            result.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        pass

    # All methods failed — return original (may have black background)
    log.warning("  SMask composite completely failed for xref=%d — image may have black bg", xref)
    return raw_bytes


# ── Image-based equation extraction (OCR-or-save) ────────────────

def _process_equation_image(pdf, img_bytes: bytes, ext: str,
                             page_num: int, counter: int, fig_dir: str,
                             bbox_y: float = 0.0) -> dict:
    """
    Process a small image as a math equation.
    Strategy: composite on white (fix SMask black bg), save, defer OCR to batch.
    Returns a formula dict with image_path always set; latex filled later by batch OCR.
    """
    if not img_bytes or len(img_bytes) < 100:
        return None

    # CRITICAL: Composite alpha onto white to fix SMask black backgrounds
    img_bytes = _composite_on_white(img_bytes)

    # Save image to figures dir WITH 200 DPI metadata
    # Without DPI, LaTeX assumes 72 DPI → images render 2-3x too large
    fname = f"eq_{page_num}_{counter}.png"
    fpath = os.path.join(fig_dir, fname)
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.save(fpath, "PNG", dpi=(200, 200))
    except Exception:
        with open(fpath, "wb") as f:
            f.write(img_bytes)

    # Return dict WITHOUT latex — OCR is deferred to batch phase
    # (see _batch_ocr_equations called after all images are collected)
    log.debug("  Equation image saved p%d: %s (%.1f KB)", page_num, fname, len(img_bytes)/1024)
    return {
        "latex": "",
        "image_path": fpath,
        "confidence": 0.5,   # medium — it IS an equation image, OCR pending
        "page": page_num,
        "label": f"eq_{page_num}_{counter}",
        "bbox_y": bbox_y,
    }


def _save_figure_dict(img_bytes: bytes, page_num: int, fig_idx: int,
                      fig_dir: str, bbox_y: float = 0.0) -> dict:
    """
    Save a figure image to disk and return a figure dict.

    Args:
        img_bytes: Raw image bytes
        page_num: Page number for naming
        fig_idx: Figure index on this page
        fig_dir: Directory to save image to
        bbox_y: Y-coordinate for ordering figures

    Returns:
        Figure dict with keys: image_path, caption, label, page, bbox_y
    """
    fname = f"fig_{page_num}_{fig_idx}.png"
    fpath = os.path.join(fig_dir, fname)
    with open(fpath, "wb") as f:
        f.write(img_bytes)
    return {
        "image_path": fpath,
        "caption": "",
        "label": f"fig_{page_num}_{fig_idx}",
        "page": page_num,
        "bbox_y": bbox_y,
    }


# ── Primary image extraction via get_images() ────────────────────

def _extract_all_page_images(pdf, page, page_num: int, fig_dir: str,
                              total_pages: int,
                              table_bboxes: list = None,
                              text_dict: dict = None,
                              skip_xrefs: set = None) -> tuple:
    """
    Extract ALL images from a page.
    Uses get_images(full=True) for image bytes, and text dict blocks for
    rendered dimensions (PDF points) — critical for correct classification.
    Skips equation images that overlap with table regions (handled by CELLIMG).
    Returns (figures_list, formulas_list).
    """
    import fitz
    if table_bboxes is None:
        table_bboxes = []

    figures = []
    formulas = []

    # Phase 1a: Build xref → rendered bbox mapping from text dict.
    # text dict type=1 blocks have bbox in PDF points (the RENDERED size).
    # pdf.extract_image() returns PIXEL dimensions (much larger) — don't use for classification.
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    xref_to_rendered = {}   # xref → (w_pts, h_pts, bbox_y, bbox)
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

    # Phase 1b: Build xref → placement bbox from get_image_info().
    # This catches images NOT in the text dict (XObjects placed via Form operators).
    # Without this, many embedded equation images get bbox_y=0.0, breaking placement.
    xref_to_placement = {}
    try:
        for info in page.get_image_info(xrefs=True):
            ix = info.get("xref", 0)
            ib = info.get("bbox")
            if ix > 0 and ib and ix not in xref_to_rendered:
                w_p = ib[2] - ib[0]
                h_p = ib[3] - ib[1]
                xref_to_placement[ix] = (w_p, h_p, ib[1], ib)
    except Exception:
        pass  # get_image_info may not be available in older PyMuPDF

    # Phase 2: Process images via get_images() (has xrefs for byte extraction)
    seen_xrefs = set()

    try:
        images = page.get_images(full=True)
    except Exception as e:
        log.debug("  get_images() failed p%d: %s", page_num, e)
        images = []

    for img_info in images:
        xref = img_info[0]
        smask_xref = img_info[1] if len(img_info) > 1 else 0  # SMask xref
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)

        # Skip recurring xrefs (logos, running headers — appear on 2+ pages)
        if skip_xrefs and xref in skip_xrefs:
            log.debug("  Skip recurring xref=%d p%d (logo/header)", xref, page_num)
            continue

        try:
            img_data = pdf.extract_image(xref)
            if not img_data or not img_data.get("image"):
                continue

            img_bytes = img_data["image"]
            ext = img_data.get("ext", "png")

            if len(img_bytes) < 100:
                continue

            # If image has SMask, composite the alpha BEFORE classification/saving
            if smask_xref and smask_xref > 0:
                img_bytes = _composite_with_smask(pdf, xref, smask_xref, img_bytes)
                ext = "png"  # composited output is always PNG

            # Use RENDERED dimensions (PDF points) for classification, not pixels
            if xref in xref_to_rendered:
                w, h, bbox_y, img_bbox = xref_to_rendered[xref]
            elif xref in xref_to_placement:
                # Found placement bbox via get_image_info() — accurate position
                w, h, bbox_y, img_bbox = xref_to_placement[xref]
            else:
                # Last resort: estimate from pixel dims (assume ~150 DPI)
                pw = img_data.get("width", 0)
                ph = img_data.get("height", 0)
                w = pw * 72.0 / 150.0   # convert pixels → approx PDF points
                h = ph * 72.0 / 150.0
                bbox_y = 0.0
                img_bbox = None

            classification = _classify_image(w, h, page_num, total_pages,
                                             bbox_y=bbox_y)

            # Caption override: if a "Figure N" / "Fig. N" text block sits
            # within ~80pt above/below the image, force classification to
            # 'figure'. This catches scientific diagrams that would otherwise
            # be misclassified as equations by size heuristics alone.
            if classification == "equation" and img_bbox:
                if _find_figure_caption_near(text_dict, img_bbox):
                    log.debug("  Reclassify p%d xref=%d: equation→figure (caption)",
                              page_num, xref)
                    classification = "figure"

            if classification == "skip":
                log.debug("  Skip image p%d xref=%d (%.0fx%.0f pt)", page_num, xref, w, h)
                continue

            elif classification == "equation":
                # Skip equations that are inside table regions — they're
                # already handled by _render_cell_image() as \CELLIMG markers
                known_bbox = (xref_to_rendered.get(xref) or xref_to_placement.get(xref))
                if known_bbox:
                    _, _, _, eq_bbox = known_bbox
                    if _bbox_overlaps_any(eq_bbox, table_bboxes):
                        log.debug("  Skip table equation p%d xref=%d (inside table)",
                                  page_num, xref)
                        continue
                elif table_bboxes:
                    # Image has no rendered bbox at all (not in text dict or placement).
                    # If this page HAS tables, skip small equation-sized images —
                    # they're almost certainly table cell equations handled by CELLIMG.
                    log.debug("  Skip equation p%d xref=%d (no bbox, page has tables)",
                              page_num, xref)
                    continue

                # ALWAYS prefer pixmap rendering of the page region.
                # This composites SMask/alpha correctly onto white background,
                # avoiding the "black spots" bug from raw extract_image() bytes.
                eq_bytes = None
                eq_ext = "png"
                if known_bbox:
                    try:
                        _, _, _, clip_bbox = known_bbox
                        mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(clip_bbox),
                                              alpha=False)  # white background
                        eq_bytes = pix.tobytes("png")
                    except Exception as e:
                        log.debug("  Pixmap render failed p%d xref=%d: %s", page_num, xref, e)

                # Fallback: composite raw bytes onto white (handles SMask)
                if not eq_bytes:
                    eq_bytes = _composite_on_white(img_bytes)
                    eq_ext = ext

                fb = _process_equation_image(
                    pdf, eq_bytes, eq_ext, page_num, len(formulas),
                    fig_dir, bbox_y=bbox_y
                )
                if fb:
                    formulas.append(fb)
                    log.debug("  Equation p%d xref=%d (%.0fx%.0f pt)",
                              page_num, xref, w, h)

            elif classification == "figure":
                # ALWAYS prefer pixmap rendering for figures too — avoids
                # black boxes from SMask, JBIG2, CMYK, exotic formats.
                fig_bytes = None
                fig_ext = "png"
                fig_known_bbox = (xref_to_rendered.get(xref) or xref_to_placement.get(xref))
                if fig_known_bbox:
                    try:
                        _, _, _, fig_clip = fig_known_bbox
                        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI for figures
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(fig_clip),
                                              alpha=False)  # white background
                        fig_bytes = pix.tobytes("png")
                    except Exception as e:
                        log.debug("  Figure pixmap render failed p%d xref=%d: %s",
                                  page_num, xref, e)

                # Fallback: composite raw bytes onto white
                if not fig_bytes:
                    fig_bytes = _composite_on_white(img_bytes)

                figures.append(_save_figure_dict(
                    fig_bytes, page_num, len(figures), fig_dir, bbox_y))
                log.debug("  Figure p%d xref=%d (%.0fx%.0f pt): %s",
                          page_num, xref, w, h,
                          f"fig_{page_num}_{len(figures)-1}.png")

        except Exception as e:
            log.debug("  Image extraction failed p%d xref=%d: %s", page_num, xref, e)

    # Phase 3: Handle inline images (no xref — render from bbox)
    for block, w, h, bbox_y in inline_blocks:
        bbox = block.get("bbox")
        if not bbox or w < MIN_IMG_SIZE or h < MIN_IMG_SIZE:
            continue

        classification = _classify_image(w, h, page_num, total_pages,
                                         bbox_y=bbox_y)
        if classification == "skip":
            continue

        try:
            mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
            pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox), alpha=False)
            img_bytes = pix.tobytes("png")

            if classification == "equation":
                fb = _process_equation_image(
                    pdf, img_bytes, "png", page_num, len(formulas),
                    fig_dir, bbox_y=bbox_y
                )
                if fb:
                    formulas.append(fb)
            elif classification == "figure":
                figures.append(_save_figure_dict(
                    img_bytes, page_num, len(figures), fig_dir, bbox_y))
        except Exception as e:
            log.debug("  Inline image failed p%d: %s", page_num, e)

    # Match equation numbers from nearby text to extracted formula images
    if formulas and text_dict:
        eq_nums = _collect_equation_numbers(text_dict)
        _match_equation_numbers(formulas, eq_nums)

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


# ── Equation number extraction ──────────────────────────────────

def _collect_equation_numbers(text_dict: dict) -> list:
    """
    Scan page text blocks for standalone equation numbers like "(7)" or "(2.1)".
    Returns list of (y_center, equation_number_str) sorted by y position.
    """
    eq_nums = []
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_text = ""
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                block_text += span.get("text", "")
        block_text = block_text.strip()
        if EQ_NUM_RE.match(block_text):
            m = _EQ_NUM_EXTRACT_RE.search(block_text)
            if m:
                bbox = block.get("bbox", [0, 0, 0, 0])
                y_center = (bbox[1] + bbox[3]) / 2
                eq_nums.append((y_center, m.group(1)))
    eq_nums.sort(key=lambda x: x[0])
    return eq_nums


def _split_block_by_formulas(block: dict, eq_y_spans: list) -> list:
    """
    Split a fitz text block into sub-blocks wherever an equation sits, OR
    wherever two consecutive lines have an abnormally large vertical gap
    (unfilled space is almost always an image — equation or figure — that
    fitz didn't merge into the block).

    Without this, PyMuPDF groups lines above + below an inline equation
    into one block, which the parser then treats as a single paragraph.
    Result: formulas have nowhere to be inserted except at section end.
    """
    lines = block.get("lines", [])
    if not lines:
        return []

    def _line_y(line):
        bb = line.get("bbox", [0, 0, 0, 0])
        if len(bb) != 4:
            return 0.0, 0.0
        return bb[1], bb[3]  # (top, bottom)

    # Compute median line height to detect abnormal gaps
    heights = []
    for line in lines:
        y0, y1 = _line_y(line)
        if y1 > y0:
            heights.append(y1 - y0)
    median_h = sorted(heights)[len(heights) // 2] if heights else 12.0
    # A "large gap" is > 1.6x the median line height — tight enough to miss
    # normal paragraph leading but catch a skipped image/equation
    gap_threshold = max(median_h * 1.6, 14.0)

    def _crosses_formula(prev_bottom: float, curr_top: float) -> bool:
        """True if any equation span lies between two consecutive lines."""
        if prev_bottom >= curr_top:
            return False
        for eq_y0, eq_y1 in eq_y_spans:
            # formula bbox_y==0 degenerate spans are skipped upstream
            if prev_bottom <= eq_y0 and eq_y1 <= curr_top:
                return True
            # Also split if the formula simply overlaps the gap region
            if eq_y0 < curr_top and eq_y1 > prev_bottom and eq_y1 > eq_y0:
                return True
        return False

    # Group lines into runs; split on formula crossings OR large line gaps
    runs = [[]]
    prev_bottom = None
    for line in lines:
        ly_top, ly_bot = _line_y(line)
        if prev_bottom is not None:
            gap = ly_top - prev_bottom
            split_here = (
                _crosses_formula(prev_bottom, ly_top)
                or gap > gap_threshold
            )
            if split_here:
                runs.append([])
        runs[-1].append(line)
        prev_bottom = ly_bot

    if len(runs) == 1:
        # No split needed — flatten back to single block
        return [_run_to_subblock(lines, block.get("bbox", [0, 0, 0, 0]))]

    # Build a sub-block per run, computing its own bbox
    sub_blocks = []
    for run in runs:
        if not run:
            continue
        sub_blocks.append(_run_to_subblock(run, None))
    return sub_blocks


def _run_to_subblock(run_lines: list, fallback_bbox) -> dict:
    """Build a sub-block dict from a list of fitz line dicts."""
    text_parts = []
    fonts = []
    sizes = []
    xs0, ys0, xs1, ys1 = [], [], [], []
    for line in run_lines:
        line_text = ""
        for span in line.get("spans", []):
            line_text += span.get("text", "")
            fonts.append(span.get("font", ""))
            sizes.append(span.get("size", 0))
        text_parts.append(line_text)
        lbb = line.get("bbox", [0, 0, 0, 0])
        if len(lbb) == 4:
            xs0.append(lbb[0]); ys0.append(lbb[1])
            xs1.append(lbb[2]); ys1.append(lbb[3])

    if xs0:
        bbox = [min(xs0), min(ys0), max(xs1), max(ys1)]
    else:
        bbox = fallback_bbox or [0, 0, 0, 0]

    font = max(set(fonts), key=fonts.count) if fonts else ""
    size = max(set(sizes), key=sizes.count) if sizes else 0
    return {
        "text": "\n".join(text_parts),
        "bbox": bbox,
        "font": font,
        "size": size,
    }


def _auto_number_formulas(formulas: list) -> None:
    """
    Assign sequential equation numbers to any formula missing one.
    Only runs when:
      - The document already has at least one matched equation number, OR
      - There are 2+ formulas (likely a formula-heavy paper)
    Numbers start from max existing number + 1 (or 1 if none).
    Order: by (page, bbox_y) reading order.
    """
    if not formulas:
        return

    matched = [f for f in formulas if f.get("equation_number")]
    if not matched and len(formulas) < 2:
        return  # document doesn't use equation numbering

    # Find next number to assign (max existing + 1, or 1)
    used_nums = set()
    max_num = 0
    for f in matched:
        try:
            n = int(str(f["equation_number"]).split(".")[0])
            used_nums.add(str(f["equation_number"]))
            if n > max_num:
                max_num = n
        except (ValueError, TypeError):
            continue

    next_num = max_num + 1 if max_num > 0 else 1

    # Sort unmatched formulas by reading order
    unmatched = [f for f in formulas if not f.get("equation_number")]
    unmatched.sort(key=lambda f: (f.get("page", 0), f.get("bbox_y", 0)))

    for f in unmatched:
        while str(next_num) in used_nums:
            next_num += 1
        f["equation_number"] = str(next_num)
        used_nums.add(str(next_num))
        next_num += 1

    log.info("  Auto-numbered %d formulas (range starting at %d)",
             len(unmatched), max_num + 1 if max_num > 0 else 1)


def _match_equation_numbers(formulas: list, eq_nums: list, max_y_dist: float = 50.0):
    """
    Match equation numbers to formula dicts by y-position proximity.
    Mutates formula dicts in-place, adding 'equation_number' key.
    Each equation number is used at most once (closest formula wins).
    """
    if not eq_nums or not formulas:
        return

    used = set()
    # Sort formulas by y for stable matching
    sorted_formulas = sorted(formulas, key=lambda f: f.get("bbox_y", 0))

    for f in sorted_formulas:
        f_y = f.get("bbox_y", 0)
        best_idx = None
        best_dist = float("inf")
        for i, (eq_y, eq_num) in enumerate(eq_nums):
            if i in used:
                continue
            dist = abs(f_y - eq_y)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx is not None and best_dist <= max_y_dist:
            f["equation_number"] = eq_nums[best_idx][1]
            used.add(best_idx)


def _match_eq_nums_from_text_blocks(text_blocks: list, formulas: list,
                                     max_y_dist: float = 80.0):
    """
    Match equation numbers from parser-style text blocks to formula dicts.

    Works with pdfplumber blocks: {"text": "...", "page": N, "bbox": [x0,y0,x1,y1]}
    Scans blocks for standalone "(N)" patterns AND inline trailing "(N)" patterns.
    Matches to nearby formulas by page + y-proximity.

    Uses formula image center (not top) for distance, with tolerance scaled by
    formula height to handle tall equations (matrices).

    Mutates formula dicts in-place, adding 'equation_number' key.
    """
    if not text_blocks or not formulas:
        return

    # Collect equation number positions from text blocks
    # Match both standalone "(N)" lines and inline trailing " (N)" at end of short lines
    # Trailing pattern requires space before ( to avoid matching σ(1)π(1) style math
    _INLINE_EQ_NUM_RE = re.compile(r'\s\((\d{1,3})\)\s*$')
    eq_nums = []  # (page, y_center, number_str)
    for blk in text_blocks:
        text = blk.get("text", "").strip()
        if not text:
            continue
        bbox = blk.get("bbox", [0, 0, 0, 0])
        page = blk.get("page", 0)
        for line in text.split("\n"):
            line_stripped = line.strip()
            # Standalone: "(N)" alone on a line
            if EQ_NUM_RE.match(line_stripped):
                m = _EQ_NUM_EXTRACT_RE.search(line_stripped)
                if m:
                    y_center = (bbox[1] + bbox[3]) / 2
                    eq_nums.append((page, y_center, m.group(1)))
            # Inline trailing: "equation text (N)" — short line ending with (N)
            # Requires space before ( to avoid math like σ(1)π(1)
            elif len(line_stripped) < 80:
                im = _INLINE_EQ_NUM_RE.search(line_stripped)
                if im:
                    y_center = (bbox[1] + bbox[3]) / 2
                    eq_nums.append((page, y_center, im.group(1)))

    if not eq_nums:
        return

    log.debug("  Found %d equation numbers in text blocks", len(eq_nums))
    for pg, yc, num in eq_nums:
        log.debug("    eq(%s) page=%d y=%.1f", num, pg, yc)

    # Match: for each formula, find the closest unused equation number on the same page
    # Use formula center y (bbox_y is top; estimate height from image if available)
    used = set()
    sorted_formulas = sorted(formulas, key=lambda f: (f.get("page", 0), f.get("bbox_y", 0)))

    for f in sorted_formulas:
        f_page = f.get("page", 0)
        f_top = f.get("bbox_y", 0)
        f_height = f.get("bbox_h", 0)
        # Use center of formula for matching (not top)
        f_center = f_top + f_height / 2 if f_height > 0 else f_top
        # Scale tolerance: tall formulas (matrices) need more distance
        effective_max = max_y_dist + f_height / 2 if f_height > 0 else max_y_dist

        best_idx = None
        best_dist = float("inf")
        for i, (eq_page, eq_y, eq_num) in enumerate(eq_nums):
            if i in used or eq_page != f_page:
                continue
            dist = abs(f_center - eq_y)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx is not None and best_dist <= effective_max:
            f["equation_number"] = eq_nums[best_idx][2]
            used.add(best_idx)
            log.debug("  Matched eq number (%s) to formula at page=%d y=%.0f (dist=%.0f, max=%.0f)",
                      eq_nums[best_idx][2], f_page, f_top, best_dist, effective_max)


# ── Formula region detection ─────────────────────────────────────

def _detect_formula_regions(page, page_num: int, text_dict: dict,
                            table_bboxes: list = None) -> list:
    """
    Detect formula regions on a page using math-char analysis.
    Renders each candidate as PNG and OCR's with pix2tex.
    Skips blocks inside table regions (already handled by CELLIMG).
    Returns list of dicts: {latex, confidence, page, label}
    """
    if table_bboxes is None:
        table_bboxes = []
    formulas = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue

        # Skip blocks inside table regions
        block_bbox = block.get("bbox", [0, 0, 0, 0])
        if table_bboxes and _bbox_overlaps_any(block_bbox, table_bboxes):
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

        # Only include if confidence is high enough
        if confidence < OCR_CONFIDENCE_THRESHOLD:
            log.debug("  Formula REJECTED p%d: conf=%.2f", page_num, confidence)
            continue

        formulas.append({
            "latex": latex,
            "confidence": confidence,
            "page": page_num,
            "label": f"eq_{page_num}_{len(formulas)+1}",
            "bbox_y": (bbox[1] + bbox[3]) / 2,
        })

    # Match nearby equation numbers (e.g. "(7)") to detected formulas
    eq_nums = _collect_equation_numbers(text_dict)
    _match_equation_numbers(formulas, eq_nums)

    return formulas


def _ocr_formula_region(page, bbox) -> str:
    """Render a page region at 200 DPI and OCR with both pix2tex + nougat, pick best."""
    # ── OCR time-budget gate ───────────────────────────────────────
    if not _ocr_budget.is_available():
        if _ocr_budget.exhausted:
            log.info("  OCR time budget (%.0fs) reached — skipping remaining formula regions "
                     "(they will render as images instead of LaTeX).", _ocr_budget.max_seconds)
        return ""
    # ─────────────────────────────────────────────────────────────

    tmp_path = None
    try:
        import fitz
        mat = fitz.Matrix(200 / 72, 200 / 72)
        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox), alpha=False)

        # Create temp file, close it FIRST, then write — avoids Windows file lock
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = f.name
        f.close()  # Close handle BEFORE writing to avoid Windows Permission Denied
        pix.save(tmp_path)

        # Run both engines, pick winner
        best_latex, best_conf = _dual_ocr_single(tmp_path)

        if best_latex and best_conf >= OCR_CONFIDENCE_THRESHOLD:
            return best_latex
        return ""

    except Exception as e:
        log.debug("  OCR region failed: %s", e)
        return ""
    finally:
        # Clean up temp file — retry with delay on Windows for locked files
        if tmp_path:
            for _ in range(3):
                try:
                    os.unlink(tmp_path)
                    break
                except OSError:
                    import time
                    time.sleep(0.1)


# ── Batch OCR for equation images ─────────────────────────────────

def _batch_ocr_equations(formulas: list) -> list:
    """
    Run BOTH pix2tex and nougat on equation images, pick the best result per formula.

    Strategy:
      1. Batch pix2tex on all images needing OCR
      2. Batch nougat on all images needing OCR
      3. For each formula, compare both results via _pick_best_ocr(), keep the winner
      4. If both batch workers fail, fall back to single-image dual OCR

    This ensures neither engine's strengths are wasted — pix2tex excels at clean
    LaTeX formatting, nougat excels at structural/contextual understanding.
    """
    need_ocr = [(i, f) for i, f in enumerate(formulas)
                if f.get("image_path") and not f.get("latex")]

    if not need_ocr:
        return formulas

    log.info("  Dual-engine batch OCR: %d equation images to process", len(need_ocr))

    image_paths = [f["image_path"] for _, f in need_ocr]

    # Run both batch workers — they're independent, both load model once
    pix2tex_results = _run_batch_ocr_worker("pix2tex", image_paths)
    nougat_results = _run_batch_ocr_worker("nougat", image_paths)

    both_batch_ok = pix2tex_results is not None or nougat_results is not None

    if both_batch_ok:
        # Normalize: if one batch failed entirely, use empty placeholders
        if pix2tex_results is None:
            pix2tex_results = [{"path": p, "latex": ""} for p in image_paths]
        if nougat_results is None:
            nougat_results = [{"path": p, "latex": ""} for p in image_paths]

        for (idx, formula), p_res, n_res in zip(need_ocr, pix2tex_results, nougat_results):
            latex_p = p_res.get("latex", "")
            latex_n = n_res.get("latex", "")

            best_latex, best_conf = _pick_best_ocr(latex_p, latex_n)

            if best_latex and best_conf >= OCR_CONFIDENCE_THRESHOLD:
                formulas[idx]["latex"] = best_latex
                formulas[idx]["confidence"] = best_conf
                # Log which engine won (compare sanitized versions)
                san_p = _sanitize_ocr_latex(latex_p) if latex_p else ""
                winner = "pix2tex" if (san_p and best_latex == san_p) else "nougat"
                log.info("  Dual OCR p%d: winner=%s conf=%.2f latex=%s",
                         formula.get("page", 0), winner, best_conf, best_latex[:60])
            else:
                san_p = _sanitize_ocr_latex(latex_p) if latex_p else ""
                san_n = _sanitize_ocr_latex(latex_n) if latex_n else ""
                conf_p = _score_ocr_quality(san_p) if san_p else 0.0
                conf_n = _score_ocr_quality(san_n) if san_n else 0.0
                log.debug("  Dual OCR REJECTED p%d: pix2tex=%.2f nougat=%.2f",
                          formula.get("page", 0), conf_p, conf_n)
    else:
        # Both batch workers unavailable — fall back to single-image dual OCR
        log.info("  Both batch workers unavailable, falling back to single-image dual OCR")
        for idx, formula in need_ocr:
            best_latex, best_conf = _dual_ocr_single(formula["image_path"])
            if best_latex and best_conf >= OCR_CONFIDENCE_THRESHOLD:
                formulas[idx]["latex"] = best_latex
                formulas[idx]["confidence"] = best_conf

    # Log summary
    ocr_success = sum(1 for f in formulas if f.get("latex"))
    img_only = sum(1 for f in formulas if f.get("image_path") and not f.get("latex"))
    log.info("  Dual OCR done: %d with LaTeX, %d image-only fallback", ocr_success, img_only)

    return formulas


def _ocr_table_cells(tables: list) -> list:
    """
    Scan all table rows/headers for \\CELLIMG{path} markers.
    Run pix2tex batch OCR on the collected images.
    Replace \\CELLIMG{path} with \\CELLEQ{latex} where OCR succeeds.

    This makes table formula cells selectable text instead of images.
    """
    import re as _re

    _cellimg_re = _re.compile(r"^\\CELLIMG\{(.+?)\}$")

    # Phase 1: collect all CELLIMG paths and their positions
    # positions: list of (table_idx, 'header'|'row', row_idx, col_idx, image_path)
    collected = []

    for ti, table in enumerate(tables):
        headers = table.get("headers", [])
        for ci, cell in enumerate(headers):
            m = _cellimg_re.match(str(cell).strip())
            if m:
                collected.append((ti, "header", 0, ci, m.group(1)))

        rows = table.get("rows", [])
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                m = _cellimg_re.match(str(cell).strip())
                if m:
                    collected.append((ti, "row", ri, ci, m.group(1)))

    if not collected:
        return tables

    log.info("  Table cell dual OCR: %d cell images to process", len(collected))

    # Table cell OCR threshold is LOWER than standalone formulas (0.40 vs 0.60)
    # because a slightly imperfect selectable formula is better than an image.
    # (TABLE_CELL_OCR_THRESHOLD is imported from core.shared)

    # Phase 2: batch OCR via BOTH engines, then compare per cell
    image_paths = [c[4] for c in collected]
    pix2tex_results = _run_batch_ocr_worker("pix2tex", image_paths)
    nougat_results = _run_batch_ocr_worker("nougat", image_paths)

    both_batch_ok = pix2tex_results is not None or nougat_results is not None
    ocr_success = 0

    if both_batch_ok:
        if pix2tex_results is None:
            pix2tex_results = [{"path": p, "latex": ""} for p in image_paths]
        if nougat_results is None:
            nougat_results = [{"path": p, "latex": ""} for p in image_paths]

        for (ti, kind, ri, ci, img_path), p_res, n_res in zip(
                collected, pix2tex_results, nougat_results):
            latex_p = p_res.get("latex", "")
            latex_n = n_res.get("latex", "")

            best_latex, best_conf = _pick_best_ocr(latex_p, latex_n)

            if best_latex and best_conf >= TABLE_CELL_OCR_THRESHOLD:
                marker = f"\\CELLEQ{{{best_latex}||IMG:{img_path}}}"
                if kind == "header":
                    tables[ti]["headers"][ci] = marker
                else:
                    tables[ti]["rows"][ri][ci] = marker
                ocr_success += 1
                log.debug("  Table cell dual OCR t%d r%d c%d: conf=%.2f latex=%s",
                          ti, ri, ci, best_conf, best_latex[:50])
            else:
                log.debug("  Table cell dual OCR REJECTED t%d r%d c%d", ti, ri, ci)
    else:
        # Both batch workers unavailable — single-image dual OCR
        log.info("  Table cell batch OCR unavailable, falling back to single-image dual OCR")
        for ti, kind, ri, ci, img_path in collected:
            best_latex, best_conf = _dual_ocr_single(img_path)
            if best_latex and best_conf >= TABLE_CELL_OCR_THRESHOLD:
                marker = f"\\CELLEQ{{{best_latex}||IMG:{img_path}}}"
                if kind == "header":
                    tables[ti]["headers"][ci] = marker
                else:
                    tables[ti]["rows"][ri][ci] = marker
                ocr_success += 1

    log.info("  Table cell dual OCR done: %d/%d converted to LaTeX (threshold=%.2f)",
             ocr_success, len(collected), TABLE_CELL_OCR_THRESHOLD)
    return tables


def _run_batch_ocr_worker(engine: str, image_paths: list) -> list:
    """
    Run batch pix2tex worker: loads model once, processes all images.
    Returns list of {"path": ..., "latex": ...} or None on failure.
    """
    if not image_paths:
        return []

    # ── OCR time-budget gate ──────────────────────────────────────
    if not _ocr_budget.is_available():
        log.info("  Batch OCR skipped — OCR budget exhausted or disabled")
        return None
    # ─────────────────────────────────────────────────────────────

    # Check if pix2tex is available
    if not _check_ocr_available(engine):
        return None

    global _PYTHON_EXE
    if _PYTHON_EXE is None:
        _PYTHON_EXE = _get_python_exe()

    worker_dir = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(worker_dir, f"{engine}_batch_worker.py")

    if not os.path.isfile(worker):
        # Fall back to single-image worker
        log.debug("  Batch worker not found: %s", worker)
        return None

    try:
        import json
        # Timeout scales with number of images:
        # 60s for model load + 15s per image (pix2tex takes 10-15s each on CPU)
        # Also cap at OCR_BUDGET_SECONDS + 60 so the batch can't run longer than the budget
        timeout = min(60 + len(image_paths) * 15, _ocr_budget.max_seconds + 60)

        result = subprocess.run(
            [_PYTHON_EXE, worker] + image_paths,
            capture_output=True,
            timeout=timeout,
        )

        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()

        if stderr:
            log.debug("  Batch %s worker stderr: %s", engine, stderr[:300])

        if result.returncode == 0 and stdout:
            try:
                results = json.loads(stdout)
                log.info("  Batch %s: processed %d images", engine, len(results))
                return results
            except json.JSONDecodeError as e:
                log.debug("  Batch %s: invalid JSON output: %s", engine, e)

    except subprocess.TimeoutExpired:
        log.warning("  Batch %s worker timed out (%d images)", engine, len(image_paths))
    except Exception as e:
        log.debug("  Batch %s worker failed: %s", engine, e)

    return None


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
    # Platform-aware: check Linux/Mac paths first on Unix, Windows first on Windows
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    is_windows = sys.platform.startswith("win")
    if is_windows:
        candidates = [
            os.path.join(project_root, "venv", "Scripts", "python.exe"),
            os.path.join(project_root, ".venv", "Scripts", "python.exe"),
            os.path.join(project_root, "venv", "bin", "python"),
            os.path.join(project_root, ".venv", "bin", "python"),
        ]
    else:
        candidates = [
            os.path.join(project_root, "venv", "bin", "python"),
            os.path.join(project_root, ".venv", "bin", "python"),
            # Skip Windows .exe on non-Windows — causes Exec format error
        ]
    for candidate in candidates:
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

        # nougat's import chain (torch+transformers+nougat) is heavy on cold start;
        # 10s is not enough and causes a false-negative that disables dual-engine OCR.
        probe_timeout = 60 if engine == "nougat" else 15
        result = subprocess.run(check_cmd, capture_output=True, timeout=probe_timeout)
        available = result.returncode == 0 and "ok" in result.stdout.decode("utf-8", errors="replace")
        _OCR_AVAIL[engine] = available
        if available:
            log.info("  OCR engine '%s' is available", engine)
        else:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            log.info("  OCR engine '%s' not available: %s", engine, stderr[:120])
    except subprocess.TimeoutExpired:
        # Don't cache a negative from the probe alone — the batch worker itself
        # is the authoritative check. Assume-available so dual-engine can try.
        log.info("  OCR engine '%s' import probe timed out (>%ds) — will still try batch worker",
                 engine, probe_timeout)
        _OCR_AVAIL[engine] = True
    except Exception as e:
        log.info("  OCR engine '%s' availability check failed: %s", engine, e)
        _OCR_AVAIL[engine] = False

    return _OCR_AVAIL.get(engine, False)


def _run_ocr_worker(engine: str, image_path: str) -> str:
    """Run pix2tex or nougat worker in a subprocess. Returns LaTeX or ''."""
    # ── OCR time-budget gate ──────────────────────────────────────
    if not _ocr_budget.is_available():
        return ""
    # ─────────────────────────────────────────────────────────────

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
    Penalizes prose-as-math, garbage OCR, and common pix2tex artifacts.
    Rewards real math structures (\\frac, ^{}, \\int, etc.)

    Score < 0.45 → rejected. Score >= 0.7 → high confidence.
    """
    if not latex:
        return 0.0

    score = 0.5

    # ── Penalties ──────────────────────────────────────────────

    # Penalize high \mathrm{...} ratio (prose wrapped as fake math)
    mathrm = re.findall(r"\\mathrm\{([^}]*)\}", latex)
    mathrm_chars = sum(len(m) for m in mathrm)
    if mathrm_chars / max(len(latex), 1) > 0.5:
        score -= 0.3
    elif mathrm_chars / max(len(latex), 1) > 0.3:
        score -= 0.15

    # Penalize tilde-separated words inside \mathrm (pix2tex space encoding)
    for m in mathrm:
        if m.count("~") >= 2:
            score -= 0.2
            break

    # Penalize \scriptstyle wrapping large blocks
    if r"\scriptstyle" in latex and len(latex) > 50:
        score -= 0.15

    # Penalize \to / \rightarrow / \leftrightarrow (pix2tex misreads = as arrows)
    # BUT don't penalize \to when inside \lim context (legitimate usage)
    arrow_count = (latex.count(r"\rightarrow")
                   + latex.count(r"\leftrightarrow") + latex.count(r"\longleftrightarrow")
                   + latex.count(r"\Leftrightarrow") + latex.count(r"\Longleftrightarrow")
                   + latex.count(r"\longrightarrow") + latex.count(r"\Longrightarrow")
                   + latex.count(r"\hookrightarrow") + latex.count(r"\Rightarrow"))
    # Count \to separately — skip if \lim present (e.g. \lim_{x \to 0})
    to_count = len(re.findall(r"\\to(?![a-zA-Z])", latex))
    if r"\lim" in latex:
        to_count = max(0, to_count - 1)  # allow one \to for the \lim
    arrow_count += to_count
    if arrow_count >= 1:
        score -= 0.15 * arrow_count

    # Penalize long runs of plain letters (not in commands) — sign of prose
    # Strip LaTeX commands first, then check remaining plain text ratio
    stripped = re.sub(r"\\[a-zA-Z]+", "", latex)  # remove commands
    stripped = re.sub(r"[{}\[\]()^_$\\]", "", stripped)  # remove structure
    plain_letters = re.findall(r"[a-zA-Z]{4,}", stripped)  # runs of 4+ letters
    if plain_letters:
        plain_total = sum(len(w) for w in plain_letters)
        if plain_total / max(len(latex), 1) > 0.4:
            score -= 0.3  # mostly prose, not math
        elif plain_total / max(len(latex), 1) > 0.25:
            score -= 0.15

    # Penalize many isolated single-letter tokens WITHOUT any = sign
    # (pix2tex garbage like "l^{*} p \eta ..." has no equation structure)
    # Real equations like "F = G \frac{m_1 m_2}{r^2}" have = signs
    if "=" not in latex:
        isolated_singles = re.findall(r"(?<![\\a-zA-Z])[a-zA-Z](?![a-zA-Z{])", stripped)
        if len(isolated_singles) >= 4:
            score -= 0.15

    # Penalize very short output (likely garbage)
    if len(latex) < 5:
        score -= 0.2

    # Penalize very long output (likely full page OCR noise)
    if len(latex) > 500:
        score -= 0.15

    # Penalize excessive \mathcal usage (pix2tex misclassifies normal letters)
    mathcal_count = latex.count(r"\mathcal")
    if mathcal_count >= 3:
        score -= 0.2
    elif mathcal_count >= 2:
        score -= 0.1

    # Penalize \Xi, \Theta etc. rare Greek in combination with other weirdness
    rare_greek = [r"\Xi", r"\Upsilon", r"\Digamma"]
    for rg in rare_greek:
        if rg in latex:
            score -= 0.1

    # Penalize outputs containing "pout", "REFERENCES", common garbage words
    garbage_words = ["pout", "REFERENCES", "References", "Abstract",
                     "Keywords", "Introduction", "equation"]
    for gw in garbage_words:
        if gw in latex:
            score -= 0.3
            break

    # Penalize Unicode arrows (↔, →, ←) — pix2tex garbage artifacts
    unicode_arrows = ["↔", "→", "←", "⟶", "⟵", "⟷"]
    for ua in unicode_arrows:
        if ua in latex:
            score -= 0.15

    # Penalize {X}↔ or {X}\to patterns — single-letter braced groups with arrows
    # Pattern like {Q}↔ Q/Q = is nonsensical
    if re.search(r"\{[A-Za-z]\}\\?(to|leftrightarrow|rightarrow|longrightarrow)", latex):
        score -= 0.2

    # Penalize equations that are just X/X = or single-var ratios
    if re.search(r"^[{\\A-Za-z]{1,5}\s*/\s*[{\\A-Za-z]{1,5}\s*=", stripped):
        score -= 0.15

    # ── pix2tex garble patterns ──────────────────────────────
    # These are structurally valid LaTeX but semantically wrong.
    # pix2tex produces them when it misreads complex formulas.

    # Comma after open paren: \Phi(,r) — syntactically broken math
    if re.search(r"\(\s*,", latex):
        score -= 0.2

    # Trivial fractions: \frac{1}{1}, \frac{0}{1} etc. — never appear in real math
    if re.search(r"\\frac\{[01]\}\{[01]\}", latex):
        score -= 0.15

    # \div inside \frac — pix2tex misreads fraction bars as \div
    if r"\div" in latex:
        score -= 0.15

    # \sim used as separator (not \sim alone) — pix2tex uses it for unrecognized symbols
    sim_count = latex.count(r"\sim")
    if sim_count >= 1:
        score -= 0.1 * sim_count

    # \ldots or ... inside \frac{} — garbled denominators/numerators
    if re.search(r"\\frac\{[^}]*\\?(?:ldots|cdots|dots|\.\.\.).*?\}", latex):
        score -= 0.2

    # \hat{\wedge} or \hat{\\cmd} — nonsensical nesting
    if re.search(r"\\hat\{\\[a-zA-Z]+\}", latex):
        score -= 0.15

    # \varrho — extremely rare in real math, common pix2tex artifact
    if r"\varrho" in latex:
        score -= 0.15

    # Arrow commands used as subscript/superscript bases — structural nonsense
    # e.g. \Longleftrightarrow_{k=1} — arrows never take subscripts in real math
    arrow_with_sub = re.findall(
        r"\\(?:Longleftrightarrow|Leftrightarrow|longleftrightarrow|leftrightarrow|"
        r"Longrightarrow|Rightarrow|rightarrow|longrightarrow)\s*[_^]\{",
        latex
    )
    if arrow_with_sub:
        score -= 0.25 * len(arrow_with_sub)

    # Consecutive digit subscripts like _{012} or _{0123} — garbage indices
    if re.search(r"_\{?\d{3,}\}?", latex):
        score -= 0.15

    # No = sign in a formula with \frac — most real equations have = somewhere
    # (e.g. F = G\frac{...}{...}) but garbled OCR often omits it
    if r"\frac" in latex and "=" not in latex and r"\int" not in latex:
        score -= 0.1

    # ── Rewards ──────────────────────────────────────────────

    # Reward real math structures
    for pat in [r"\frac", r"\int", r"\sum", r"\prod", r"\lim",
                "^{", "_{", r"\alpha", r"\beta", r"\partial",
                r"\sqrt", r"\infty", r"\pi", r"\sigma", r"\omega"]:
        if pat in latex:
            score += 0.05
    if re.search(r"[+\-=<>]", latex):
        score += 0.05

    # Bonus for balanced equations (has = sign with stuff on both sides)
    if re.search(r".+=.+", stripped):
        score += 0.05

    return max(0.0, min(1.0, score))


# ── Dual-engine OCR comparison ──────────────────────────────────

def _pick_best_ocr(latex_a: str, latex_b: str) -> tuple:
    """
    Compare two OCR results (from pix2tex and nougat), return the best one.
    Both inputs should already be sanitized via _sanitize_ocr_latex().

    Returns (best_latex: str, best_confidence: float).
    If both are empty/invalid, returns ("", 0.0).
    """
    # Sanitize + score both
    results = []
    for latex in (latex_a, latex_b):
        if not latex:
            results.append(("", 0.0))
            continue
        clean = _sanitize_ocr_latex(latex)
        if not clean:
            results.append(("", 0.0))
            continue
        conf = _score_ocr_quality(clean)
        results.append((clean, conf))

    (la, ca), (lb, cb) = results

    # Both empty
    if not la and not lb:
        return ("", 0.0)
    # One empty
    if not la:
        return (lb, cb)
    if not lb:
        return (la, ca)
    # Both have results — pick the higher confidence
    if ca >= cb:
        return (la, ca)
    return (lb, cb)


def _dual_ocr_single(image_path: str) -> tuple:
    """
    Run both pix2tex and nougat on a single image, return the best result.
    Returns (latex: str, confidence: float).
    """
    latex_p = _run_ocr_worker("pix2tex", image_path)
    latex_n = _run_ocr_worker("nougat", image_path)
    return _pick_best_ocr(latex_p, latex_n)


def _capture_unmatched_equation_regions(pdf, all_blocks, all_formulas,
                                        fig_dir, total_pages):
    """
    Find equation numbers (N) that weren't matched to any formula block.
    For each, render the page region around the equation number as an image.

    This handles equations that are a mix of text + vector graphics (e.g. fractions)
    where the embedded image is too small to classify as an equation.
    """
    import io

    # Collect which equation numbers are already matched
    matched_nums = {f.get("equation_number", "") for f in all_formulas
                    if f.get("equation_number")}

    # Also check if any matched formulas have very small images that need
    # region capture (e.g. π = O/2r where only the fraction is an image)
    SMALL_IMAGE_AREA = 2000
    small_matched = set()
    for f in all_formulas:
        eq_num = f.get("equation_number", "")
        bh = f.get("bbox_h", 0)
        bw = f.get("bbox_w", 0) or bh  # fallback to height as proxy
        if eq_num and bh > 0 and bh * bw < SMALL_IMAGE_AREA:
            small_matched.add(eq_num)

    # Collect STANDALONE equation numbers — "(N)" that appear as the ONLY
    # content on their line AND whose parent block has no other equation text.
    # If the block contains "A ⊗ x = b\n(2)", the normalizer handles it as text.
    # Only capture regions when (N) is truly isolated (equation is image-based).
    # Also capture regions for equation numbers matched to very small images.
    _MATH_INDICATOR = re.compile(r'[=<>≤≥≈∼⊗⊕±×÷∈∉]')
    eq_num_positions = []  # (page, y_center, num_str)
    for blk in all_blocks:
        text = blk.get("text", "").strip()
        bbox = blk.get("bbox", [0, 0, 0, 0])
        page_num = blk.get("page", 0)
        lines = text.split("\n")
        for line in lines:
            line_s = line.strip()
            if EQ_NUM_RE.match(line_s):
                m = _EQ_NUM_EXTRACT_RE.search(line_s)
                if m:
                    num = m.group(1)
                    # Check if other lines in this block have math content
                    # If so, the normalizer will handle this as a text equation
                    other_lines = [l.strip() for l in lines if l.strip() != line_s]
                    has_math_sibling = any(_MATH_INDICATOR.search(l) for l in other_lines)
                    if has_math_sibling:
                        continue  # normalizer handles this
                    if num not in matched_nums or num in small_matched:
                        y_center = (bbox[1] + bbox[3]) / 2
                        eq_num_positions.append((page_num, y_center, num))

    if not eq_num_positions:
        return all_formulas

    log.debug("  %d unmatched equation numbers — rendering page regions",
              len(eq_num_positions))

    pages = pdf.pages
    for page_num, y_center, num in eq_num_positions:
        if page_num >= len(pages):
            continue
        page = pages[page_num]
        page_w = page.width or 612
        page_h = page.height or 792

        # Crop a region: horizontal center of page, vertically around eq number
        # The equation text is typically above the (N) number
        margin_x = 70  # left margin
        crop_y0 = max(0, y_center - 50)
        crop_y1 = min(page_h, y_center + 15)
        crop_x0 = margin_x
        crop_x1 = page_w - margin_x  # exclude the (N) itself on right margin

        try:
            cropped = page.crop((crop_x0, crop_y0, crop_x1, crop_y1))
            pil_img = cropped.to_image(resolution=200)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            fname = f"eq_{page_num}_region_{num}.png"
            fpath = os.path.join(fig_dir, fname)
            with open(fpath, "wb") as f:
                f.write(img_bytes)

            new_fb = {
                "latex": "",
                "image_path": fpath,
                "confidence": 0.5,
                "page": page_num,
                "label": f"eq_{page_num}_r{num}",
                "bbox_y": crop_y0,
                "bbox_h": crop_y1 - crop_y0,
                "equation_number": num,
            }
            # If replacing a small matched image, remove the old one
            replaced = False
            if num in small_matched:
                for i, f in enumerate(all_formulas):
                    if f.get("equation_number") == num:
                        all_formulas[i] = new_fb
                        replaced = True
                        break
            if not replaced:
                all_formulas.append(new_fb)
            log.debug("  Captured equation region for (%s) p%d y=%.0f → %s",
                      num, page_num, y_center, fname)
        except Exception as e:
            log.debug("  Failed to capture equation region (%s) p%d: %s",
                      num, page_num, e)

    return all_formulas


# ══════════════════════════════════════════════════════════════════
#  Image validation — remove corrupt PNGs that would crash pdflatex
# ══════════════════════════════════════════════════════════════════

def _is_valid_png(fpath: str) -> bool:
    """Verify a PNG can be fully decoded. Catches truncated/corrupt files."""
    if not fpath or not os.path.isfile(fpath):
        return False
    try:
        from PIL import Image
        with Image.open(fpath) as img:
            img.load()  # force full decode
        return True
    except Exception:
        return False


def _validate_formula_images(formulas: list) -> list:
    """Remove formula blocks whose image_path points to a corrupt PNG."""
    valid = []
    for fb in formulas:
        img = fb.get("image_path", "")
        if img and not _is_valid_png(img):
            log.warning("  Dropping formula with corrupt image: %s",
                        os.path.basename(img))
            continue
        valid.append(fb)
    return valid


def _validate_figure_images(figures: list) -> list:
    """Remove figures whose image_path points to a corrupt PNG."""
    valid = []
    for fig in figures:
        img = fig.get("image_path", "") if isinstance(fig, dict) else getattr(fig, "image_path", "")
        if img and not _is_valid_png(img):
            log.warning("  Dropping figure with corrupt image: %s",
                        os.path.basename(img))
            continue
        valid.append(fig)
    return valid


_CELLIMG_INLINE = re.compile(r"\\CELLIMG\{([^}]+)\}")


def _validate_table_cell_images(tables: list) -> list:
    """
    Scan all table cells for \\CELLIMG{path} markers. If the referenced PNG
    is corrupt or missing, replace the marker with an empty string so
    pdflatex won't crash.
    """
    for tbl in tables:
        for key in ("headers", "rows"):
            rows = tbl.get(key)
            if not rows:
                continue
            # headers is a flat list, rows is a list of lists
            if key == "headers":
                iterable = [rows]
            else:
                iterable = rows
            for row in iterable:
                for i, cell in enumerate(row):
                    if not cell or "\\CELLIMG{" not in cell:
                        continue
                    def _replace(m):
                        p = m.group(1)
                        if _is_valid_png(p):
                            return m.group(0)
                        log.warning("  Dropping corrupt table cell image: %s",
                                    os.path.basename(p))
                        return ""
                    row[i] = _CELLIMG_INLINE.sub(_replace, cell)
    return tables


# ══════════════════════════════════════════════════════════════════
#  pdfplumber path — FALLBACK (text + tables only, no images/formulas)
# ══════════════════════════════════════════════════════════════════

def _extract_with_pdfplumber(path: str) -> dict:
    """
    Extract using pdfplumber — includes image extraction via page.crop().to_image().
    This path is used when PyMuPDF is not available.
    """
    import pdfplumber

    fig_dir = _ensure_fig_dir()
    all_text = []
    all_blocks = []
    all_tables = []
    all_figures = []
    all_formulas = []

    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)

        for page_num, page in enumerate(pdf.pages):

            # Build blocks from character-level y-position clustering
            # Get table bboxes FIRST so we can filter text blocks and images
            # that fall inside table regions (prevents table content from
            # leaking into body text as prose duplicates).
            page_table_bboxes = []
            try:
                tables = page.find_tables()
                if tables:
                    for t in tables:
                        page_table_bboxes.append(t.bbox)
            except Exception:
                pass

            page_blocks, page_text = _build_blocks_from_chars(page, page_num)

            # Filter blocks whose vertical center falls inside a table region.
            # Blocks from _build_blocks_from_chars use full page width, so area
            # overlap isn't reliable — check y-range containment instead.
            if page_table_bboxes and page_blocks:
                filtered = []
                for b in page_blocks:
                    bb = b.get("bbox") or [0, 0, 0, 0]
                    if len(bb) != 4:
                        filtered.append(b)
                        continue
                    _, by0, _, by1 = bb
                    y_center = (by0 + by1) / 2
                    in_table = False
                    for tx0, ty0, tx1, ty1 in page_table_bboxes:
                        if ty0 <= y_center <= ty1:
                            in_table = True
                            break
                    if in_table:
                        continue  # inside a table — skip
                    filtered.append(b)
                page_blocks = filtered

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

            try:
                page_images = page.images or []
                eq_counter = 0
                fig_counter = 0
                for img_meta in page_images:
                    x0 = img_meta.get("x0", 0)
                    y0 = img_meta.get("top", 0)
                    x1 = img_meta.get("x1", 0)
                    y1 = img_meta.get("bottom", 0)
                    w = x1 - x0
                    h = y1 - y0

                    classification = _classify_image(w, h, page_num, total_pages,
                                                     bbox_y=y0)
                    if classification == "skip":
                        continue

                    # Position-aware logo skip: on page 0, skip small images in
                    # the top 20% of the page (journal logos, mastheads)
                    page_h = page.height or 792
                    if page_num == 0 and y0 < page_h * 0.2:
                        area = w * h
                        if area < 50000 and h < 200:
                            log.debug("  pdfplumber: skipping page-0 header image at y=%.0f (%.0fx%.0f)", y0, w, h)
                            continue

                    # Skip equation images inside tables (already handled by CELLIMG)
                    if classification == "equation" and page_table_bboxes:
                        img_bbox = (x0, y0, x1, y1)
                        if _bbox_overlaps_any(img_bbox, page_table_bboxes, threshold=0.3):
                            log.debug("  pdfplumber: skipping equation image in table p%d (%.0fx%.0f)", page_num, w, h)
                            continue

                    # Render the image region from the page (clean white background)
                    try:
                        # Small padding around the crop
                        pad = 2
                        crop_box = (
                            max(0, x0 - pad),
                            max(0, y0 - pad),
                            min(page.width or 612, x1 + pad),
                            min(page.height or 792, y1 + pad),
                        )
                        cropped = page.crop(crop_box)
                        pil_img = cropped.to_image(resolution=200)

                        # Save as PNG
                        buf = io.BytesIO()
                        pil_img.save(buf, format="PNG")
                        img_bytes = buf.getvalue()

                        if classification == "equation":
                            fname = f"eq_{page_num}_{eq_counter}.png"
                            fpath = os.path.join(fig_dir, fname)
                            with open(fpath, "wb") as f:
                                f.write(img_bytes)
                            all_formulas.append({
                                "latex": "",
                                "image_path": fpath,
                                "confidence": 0.5,
                                "page": page_num,
                                "label": f"eq_{page_num}_{eq_counter}",
                                "bbox_y": y0,
                                "bbox_h": h,
                                "bbox_w": w,
                            })
                            eq_counter += 1
                            log.debug("  pdfplumber equation p%d: %s (%.0fx%.0f)",
                                      page_num, fname, w, h)

                        elif classification == "figure":
                            all_figures.append(_save_figure_dict(
                                img_bytes, page_num, fig_counter, fig_dir, y0))
                            fig_counter += 1

                    except Exception as e:
                        log.debug("  pdfplumber image render failed p%d: %s", page_num, e)

            except Exception as e:
                log.debug("  pdfplumber image extraction error p%d: %s", page_num, e)

            # Tables — use find_tables() for cell-level bboxes so we can
            # detect equation images inside cells and emit \CELLIMG{} markers.
            try:
                found_tables = page.find_tables() or []
                page_images = page.images or []
                for table_idx, table in enumerate(found_tables):
                    td = table.extract()
                    if not td or len(td) < 2:
                        continue

                    cells = table.cells  # (x0, top, x1, bottom) tuples
                    num_rows = len(td)
                    num_cols = max(len(row) for row in td) if td else 0
                    cell_bboxes = _build_cell_bbox_grid(cells, num_rows, num_cols)

                    processed_rows = []
                    for row_idx, row in enumerate(td):
                        processed_row = []
                        for col_idx, cell_text in enumerate(row):
                            cell_str = str(cell_text) if cell_text else ""
                            # Check every cell for an overlapping image — cell
                            # text from pdfplumber is unreliable when the cell
                            # contains an equation graphic.
                            if cell_bboxes:
                                bbox = cell_bboxes.get((row_idx, col_idx))
                                if bbox:
                                    img_path = _render_cell_image(
                                        page, bbox, page_images,
                                        page_num, table_idx, row_idx, col_idx,
                                        fig_dir,
                                    )
                                    if img_path and _is_valid_png(img_path):
                                        cell_str = f"\\CELLIMG{{{img_path}}}"
                            processed_row.append(cell_str)
                        processed_rows.append(processed_row)

                    all_tables.append({
                        "headers": processed_rows[0],
                        "rows": processed_rows[1:],
                        "caption": "",
                        "label":   f"tab_{page_num}_{len(all_tables)+1}",
                    })
            except Exception as e:
                log.debug("  pdfplumber table error p%d: %s", page_num, e)

        # ── Match equation numbers to formulas (from text blocks) ─────
        # Scan all_blocks for standalone "(N)" patterns and match to nearby formulas
        _match_eq_nums_from_text_blocks(all_blocks, all_formulas)

        # ── Capture equation regions for unmatched equation numbers ────
        # If an equation number (N) exists but no formula block matched it, the equation
        # is likely a mix of text + vector graphics (e.g. π = O/2r with a fraction image).
        # Render the region around the equation number as a cropped image.
        all_formulas = _capture_unmatched_equation_regions(
            pdf, all_blocks, all_formulas, fig_dir, total_pages)

    # ── Batch OCR on all collected equation images ────────────────
    all_formulas = _batch_ocr_equations(all_formulas)

    # OCR table cell images → convert \CELLIMG{} to \CELLEQ{latex} for selectability
    all_tables = _ocr_table_cells(all_tables)

    # ── Validate all saved PNGs — remove corrupt images ──────────
    all_formulas = _validate_formula_images(all_formulas)
    all_figures = _validate_figure_images(all_figures)

    full_text = "\n\n".join(all_text)
    log.info("  Extracted %d chars, %d blocks, %d tables, %d figures, %d formulas (pdfplumber)",
             len(full_text), len(all_blocks), len(all_tables),
             len(all_figures), len(all_formulas))

    return {
        "text": full_text,
        "blocks": all_blocks,
        "tables": all_tables,
        "figures": all_figures,
        "formula_blocks": all_formulas,
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

    # Determine dominant body text size (most common char size)
    size_counts = {}
    for c in chars:
        sz = round(c.get("size", 0), 0)
        if sz > 0:
            size_counts[sz] = size_counts.get(sz, 0) + 1
    body_size = max(size_counts, key=size_counts.get) if size_counts else 11

    # Group chars into lines by y-position (rounded to 1dp)
    lines_by_y = {}
    for c in chars:
        y = round(c.get("top", 0), 1)
        lines_by_y.setdefault(y, []).append(c)

    # Merge subscript/superscript lines into their parent body lines.
    # A sub/super line has smaller chars (< 0.8× body size) and sits within
    # half a line height of a body-size line.
    sorted_ys = sorted(lines_by_y.keys())
    merged_lines = {}  # y → list of (char, role) where role is "body"|"sub"|"super"
    sub_super_ys = set()

    for y in sorted_ys:
        line_chars = lines_by_y[y]
        avg_sz = sum(c.get("size", body_size) for c in line_chars) / len(line_chars)
        if avg_sz < body_size * 0.8:
            # This is a subscript or superscript line — find closest body line
            best_parent = None
            best_dist = float("inf")
            for py in sorted_ys:
                if py == y:
                    continue
                p_chars = lines_by_y[py]
                p_avg_sz = sum(c.get("size", body_size) for c in p_chars) / len(p_chars)
                if p_avg_sz >= body_size * 0.8:  # parent must be body-size
                    dist = abs(py - y)
                    if dist < best_dist and dist < avg_line_h * 0.8:
                        best_dist = dist
                        best_parent = py
            if best_parent is not None:
                role = "sub" if y > best_parent else "super"
                merged_lines.setdefault(best_parent, []).extend(
                    [(c, role) for c in line_chars])
                sub_super_ys.add(y)
                continue
        # Body line or unmatched sub/super — treat as body
        merged_lines.setdefault(y, []).extend(
            [(c, "body") for c in line_chars])

    sorted_ys = sorted(y for y in sorted_ys if y not in sub_super_ys)
    line_texts = []   # (y_top, line_size, line_text)
    for y in sorted_ys:
        # Use merged chars (body + sub/super) sorted by x-position
        all_chars = merged_lines.get(y, [(c, "body") for c in lines_by_y[y]])
        all_chars.sort(key=lambda cr: cr[0].get("x0", 0))

        # Build line text, wrapping sub/super groups in $...$
        # Strategy: collect tokens, then post-process to wrap math groups
        tokens = []  # list of (text, role) tuples
        prev_x1 = None
        sizes = []
        for c, role in all_chars:
            ch = c.get("text", "")
            if not ch:
                continue
            x0 = c.get("x0", 0)
            sz = c.get("bottom", 0) - c.get("top", 0)
            if sz > 0:
                sizes.append(sz)
            need_space = prev_x1 is not None and (x0 - prev_x1) > space_threshold
            if need_space:
                tokens.append((" ", "space"))
            tokens.append((ch, role))
            prev_x1 = c.get("x1", x0 + avg_w)

        # Build line text with $base_{sub}$ / $base^{super}$ wrapping
        line_text = ""
        i = 0
        while i < len(tokens):
            ch, role = tokens[i]
            if role in ("sub", "super"):
                # Collect the subscript/superscript run
                op = "_" if role == "sub" else "^"
                sub_text = ""
                while i < len(tokens) and tokens[i][1] == role:
                    sub_text += tokens[i][0]
                    i += 1
                # Find the base character (last non-space body char before this)
                base = ""
                if line_text and line_text[-1] not in (" ", "\n"):
                    # Pull back the last body character(s) as the base
                    # For single chars like 't', 'a', 'A' — pull one char
                    base = line_text[-1]
                    line_text = line_text[:-1]
                # Wrap in math mode: $base_{sub}$ or $base^{super}$
                line_text += f"${base}{op}{{{sub_text.strip()}}}$"
            else:
                line_text += ch
                i += 1

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
    """
    Extract tables using pdfplumber, with image-cell detection.

    Some table cells contain equation images instead of text. pdfplumber's
    extract_tables() returns empty strings for these cells. This function:
    1. Extracts tables normally
    2. Uses find_tables() to get cell-level bounding boxes
    3. For empty cells that overlap with page images, renders the cell
       region as a PNG and stores the image path in the cell text
    4. Populates the pdfplumber cache with both tables and bboxes
       (avoids double-opening the PDF when _get_table_bboxes is called)
    """
    if not _HAS_PDFPLUMBER:
        return []

    fig_dir = _ensure_fig_dir()
    tables = []
    bboxes_by_page = {}  # page_num -> [bbox_tuple, ...]

    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                try:
                    found_tables = page.find_tables() or []
                    page_images = page.images or []

                    # Store bboxes for this page (used by _get_table_bboxes cache)
                    if found_tables:
                        bboxes_by_page[page_num] = [t.bbox for t in found_tables]

                    for table_idx, table in enumerate(found_tables):
                        td = table.extract()
                        if not td or len(td) < 2:
                            continue

                        # Get cell bboxes from the table object
                        cells = table.cells  # list of (x0, top, x1, bottom) tuples

                        # Build a grid of cell bboxes matching the row/col structure
                        num_rows = len(td)
                        num_cols = max(len(row) for row in td) if td else 0

                        # Organize cells into a row×col grid by sorting
                        # cells are sorted top-to-bottom, left-to-right
                        cell_bboxes = _build_cell_bbox_grid(cells, num_rows, num_cols)

                        # Process each cell: check ALL cells for overlapping images.
                        # pdfplumber often extracts garbled Unicode text from cells
                        # that contain equation images. The image is the real content;
                        # the text is unreliable. Always prefer image/OCR path when
                        # an image overlaps with the cell.
                        processed_rows = []
                        for row_idx, row in enumerate(td):
                            processed_row = []
                            for col_idx, cell_text in enumerate(row):
                                cell_str = str(cell_text) if cell_text else ""

                                # Always check for overlapping images in every cell
                                if cell_bboxes:
                                    bbox = cell_bboxes.get((row_idx, col_idx))
                                    if bbox:
                                        img_path = _render_cell_image(
                                            page, bbox, page_images,
                                            page_num, table_idx, row_idx, col_idx,
                                            fig_dir
                                        )
                                        if img_path and _is_valid_png(img_path):
                                            # Image found & valid — use it instead of garbled text
                                            cell_str = f"\\CELLIMG{{{img_path}}}"
                                            log.debug("  Table cell image p%d t%d r%d c%d: %s",
                                                      page_num, table_idx, row_idx, col_idx,
                                                      os.path.basename(img_path))

                                processed_row.append(cell_str)
                            processed_rows.append(processed_row)

                        tables.append({
                            "headers": processed_rows[0],
                            "rows": processed_rows[1:],
                            "caption": "",
                            "label": f"tab_{page_num}_{len(tables)+1}",
                        })

                except Exception as e:
                    log.debug("  pdfplumber table error p%d: %s", page_num, e)
    except Exception as e:
        log.warning("  pdfplumber failed: %s", e)

    # Cache the tables and bboxes so _get_table_bboxes doesn't reopen the PDF
    _pdfplumber_cache[path] = (tables, bboxes_by_page)

    return tables


def _build_cell_bbox_grid(cells: list, num_rows: int, num_cols: int) -> dict:
    """
    Build a (row, col) → bbox mapping from pdfplumber's flat cell list.
    Cells from find_tables() are (x0, top, x1, bottom) sorted top→bottom, left→right.
    """
    if not cells:
        return {}

    # Get unique y-positions (rows) and x-positions (cols)
    y_vals = sorted(set(round(c[1], 1) for c in cells))
    x_vals = sorted(set(round(c[0], 1) for c in cells))

    grid = {}
    for cell_bbox in cells:
        x0, top, x1, bottom = cell_bbox
        # Find row index: closest y_val
        row = min(range(len(y_vals)), key=lambda i: abs(y_vals[i] - round(top, 1)))
        # Find col index: closest x_val
        col = min(range(len(x_vals)), key=lambda i: abs(x_vals[i] - round(x0, 1)))
        grid[(row, col)] = (x0, top, x1, bottom)

    return grid


def _render_cell_image(page, cell_bbox, page_images, page_num, table_idx,
                       row_idx, col_idx, fig_dir) -> str:
    """
    Check if a table cell bbox overlaps with any page image.
    If so, render the cell region as PNG and return the path.
    Returns '' if no image overlaps.
    """
    cx0, ctop, cx1, cbottom = cell_bbox
    cell_area = max((cx1 - cx0) * (cbottom - ctop), 1)

    # Check if any page image overlaps with this cell
    has_image = False
    for img in page_images:
        ix0 = img.get("x0", 0)
        iy0 = img.get("top", 0)
        ix1 = img.get("x1", 0)
        iy1 = img.get("bottom", 0)

        # Compute intersection
        ox0 = max(cx0, ix0)
        oy0 = max(ctop, iy0)
        ox1 = min(cx1, ix1)
        oy1 = min(cbottom, iy1)

        if ox0 < ox1 and oy0 < oy1:
            overlap_area = (ox1 - ox0) * (oy1 - oy0)
            img_area = max((ix1 - ix0) * (iy1 - iy0), 1)
            # Image must significantly overlap with cell (≥30% of image area)
            if overlap_area / img_area >= 0.3:
                has_image = True
                break

    if not has_image:
        return ""

    # Render the cell region as PNG
    # INSET the crop by 2pt to exclude table border/grid lines
    try:
        inset = 2  # inset to exclude table borders
        crop_box = (
            min(cx0 + inset, cx1),
            min(ctop + inset, cbottom),
            max(cx1 - inset, cx0),
            max(cbottom - inset, ctop),
        )
        cropped = page.crop(crop_box)
        pil_img = cropped.to_image(resolution=200)

        fname = f"tcell_{page_num}_{table_idx}_{row_idx}_{col_idx}.png"
        fpath = os.path.join(fig_dir, fname)

        # pil_img from pdfplumber is a CroppedPageImage, get PIL Image
        try:
            actual_img = pil_img.original  # pdfplumber's underlying PIL Image
        except AttributeError:
            actual_img = pil_img
        buf = io.BytesIO()
        actual_img.save(buf, format="PNG", dpi=(200, 200))
        with open(fpath, "wb") as f:
            f.write(buf.getvalue())

        return fpath

    except Exception as e:
        log.debug("  Cell image render failed p%d t%d r%d c%d: %s",
                  page_num, table_idx, row_idx, col_idx, e)
        return ""