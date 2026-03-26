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
        # Pass table bboxes so equation images inside tables are skipped
        # (they're already handled by _render_cell_image in table extraction)
        page_figs, page_eqs = _extract_all_page_images(
            pdf, page, page_num, fig_dir, total_pages,
            table_bboxes=page_table_bboxes
        )
        all_figures.extend(page_figs)
        all_formulas.extend(page_eqs)

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

    fig_counter = len(all_figures)

    pdf.close()

    # ── Batch OCR: run pix2tex once for ALL equation images ──────
    # This loads the model once instead of per-equation (10x faster)
    all_formulas = _batch_ocr_equations(all_formulas)

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


# ── Alpha compositing helper (fixes SMask black backgrounds) ──────

def _composite_on_white(img_bytes: bytes) -> bytes:
    """
    Composite an image with alpha/transparency onto a white background.
    Many PDF equation images use SMask (alpha mask). When extracted raw,
    transparent areas become black. This composites onto white → clean image.
    Returns PNG bytes. Falls back to original bytes on error.
    """
    try:
        from PIL import Image
        import io

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
            from PIL import Image
            import io

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
        from PIL import Image
        import io

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

    # Save image to figures dir
    fname = f"eq_{page_num}_{counter}.png"
    fpath = os.path.join(fig_dir, fname)
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


# ── Primary image extraction via get_images() ────────────────────

def _extract_all_page_images(pdf, page, page_num: int, fig_dir: str,
                              total_pages: int,
                              table_bboxes: list = None) -> tuple:
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
        smask_xref = img_info[1] if len(img_info) > 1 else 0  # SMask xref
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

            # If image has SMask, composite the alpha BEFORE classification/saving
            if smask_xref and smask_xref > 0:
                img_bytes = _composite_with_smask(pdf, xref, smask_xref, img_bytes)
                ext = "png"  # composited output is always PNG

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
                # Skip equations that are inside table regions — they're
                # already handled by _render_cell_image() as \CELLIMG markers
                if xref in xref_to_rendered:
                    _, _, _, eq_bbox = xref_to_rendered[xref]
                    if _bbox_overlaps_any(eq_bbox, table_bboxes):
                        log.debug("  Skip table equation p%d xref=%d (inside table)",
                                  page_num, xref)
                        continue
                elif table_bboxes:
                    # Image has no rendered bbox (embedded as XObject in table).
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
                if xref in xref_to_rendered:
                    try:
                        mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(img_bbox),
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
                if xref in xref_to_rendered:
                    try:
                        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI for figures
                        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(img_bbox),
                                              alpha=False)  # white background
                        fig_bytes = pix.tobytes("png")
                    except Exception as e:
                        log.debug("  Figure pixmap render failed p%d xref=%d: %s",
                                  page_num, xref, e)

                # Fallback: composite raw bytes onto white
                if not fig_bytes:
                    fig_bytes = _composite_on_white(img_bytes)

                fname = f"fig_{page_num}_{len(figures)}.png"
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
        if confidence < 0.60:
            log.debug("  Formula REJECTED p%d: conf=%.2f", page_num, confidence)
            continue

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
        pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox), alpha=False)

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


# ── Batch OCR for equation images ─────────────────────────────────

def _batch_ocr_equations(formulas: list) -> list:
    """
    Run pix2tex on ALL collected equation images in a single subprocess.
    Loads the model once → processes all images → returns updated formulas.
    Falls back to single-image nougat/tesseract workers if batch fails.
    """
    # Collect formulas that need OCR (have image_path, no latex yet)
    need_ocr = [(i, f) for i, f in enumerate(formulas)
                if f.get("image_path") and not f.get("latex")]

    if not need_ocr:
        return formulas

    log.info("  Batch OCR: %d equation images to process", len(need_ocr))

    image_paths = [f["image_path"] for _, f in need_ocr]

    # Try batch pix2tex first
    batch_results = _run_batch_ocr_worker("pix2tex", image_paths)

    if batch_results:
        # Apply results back to formula dicts
        for (idx, formula), result in zip(need_ocr, batch_results):
            latex = result.get("latex", "")
            if latex:
                latex = _sanitize_ocr_latex(latex)
            if latex:
                confidence = _score_ocr_quality(latex)
                if confidence >= 0.60:
                    formulas[idx]["latex"] = latex
                    formulas[idx]["confidence"] = confidence
                    log.debug("  Batch OCR p%d: conf=%.2f latex=%s",
                              formula.get("page", 0), confidence, latex[:60])
                else:
                    # Low confidence — keep image_path fallback, don't use OCR
                    log.debug("  Batch OCR REJECTED p%d: conf=%.2f latex=%s",
                              formula.get("page", 0), confidence, latex[:40])
    else:
        # Batch failed — fall back to single-image workers
        log.info("  Batch OCR unavailable, falling back to single-image workers")
        for idx, formula in need_ocr:
            fpath = formula["image_path"]
            latex = _run_ocr_worker("pix2tex", fpath)
            if not latex:
                latex = _run_ocr_worker("nougat", fpath)
            if latex:
                latex = _sanitize_ocr_latex(latex)
            if latex:
                confidence = _score_ocr_quality(latex)
                if confidence >= 0.60:
                    formulas[idx]["latex"] = latex
                    formulas[idx]["confidence"] = confidence

    # Log summary
    ocr_success = sum(1 for f in formulas if f.get("latex"))
    img_only = sum(1 for f in formulas if f.get("image_path") and not f.get("latex"))
    log.info("  Batch OCR done: %d with LaTeX, %d image-only fallback", ocr_success, img_only)

    return formulas


def _run_batch_ocr_worker(engine: str, image_paths: list) -> list:
    """
    Run batch pix2tex worker: loads model once, processes all images.
    Returns list of {"path": ..., "latex": ...} or None on failure.
    """
    if not image_paths:
        return []

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
        # 60s for model load + 5s per image
        timeout = 60 + len(image_paths) * 5

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

    # Penalize \to / \rightarrow (pix2tex often misreads = as →)
    arrow_count = latex.count(r"\to") + latex.count(r"\rightarrow")
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
            page_blocks, page_text = _build_blocks_from_chars(page, page_num)

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

            # ── Extract images via pdfplumber ────────────────────────
            # Uses page.crop(bbox).to_image() which renders the region
            # correctly with white background (no SMask/alpha issues)
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

                    classification = _classify_image(w, h, page_num, total_pages)
                    if classification == "skip":
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
                        import io
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
                            })
                            eq_counter += 1
                            log.debug("  pdfplumber equation p%d: %s (%.0fx%.0f)",
                                      page_num, fname, w, h)

                        elif classification == "figure":
                            fname = f"fig_{page_num}_{fig_counter}.png"
                            fpath = os.path.join(fig_dir, fname)
                            with open(fpath, "wb") as f:
                                f.write(img_bytes)
                            all_figures.append({
                                "image_path": fpath,
                                "caption": "",
                                "label": f"fig_{page_num}_{fig_counter}",
                            })
                            fig_counter += 1

                    except Exception as e:
                        log.debug("  pdfplumber image render failed p%d: %s", page_num, e)

            except Exception as e:
                log.debug("  pdfplumber image extraction error p%d: %s", page_num, e)

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

    # ── Batch OCR on all collected equation images ────────────────
    all_formulas = _batch_ocr_equations(all_formulas)

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
    """
    Extract tables using pdfplumber, with image-cell detection.

    Some table cells contain equation images instead of text. pdfplumber's
    extract_tables() returns empty strings for these cells. This function:
    1. Extracts tables normally
    2. Uses find_tables() to get cell-level bounding boxes
    3. For empty cells that overlap with page images, renders the cell
       region as a PNG and stores the image path in the cell text
    """
    if not _HAS_PDFPLUMBER:
        return []

    fig_dir = _ensure_fig_dir()
    tables = []

    try:
        import pdfplumber
        import io

        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                try:
                    found_tables = page.find_tables() or []
                    page_images = page.images or []

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

                        # Process each cell: fill empty cells with equation images
                        processed_rows = []
                        for row_idx, row in enumerate(td):
                            processed_row = []
                            for col_idx, cell_text in enumerate(row):
                                cell_str = str(cell_text) if cell_text else ""

                                # If cell is empty or very short, check for overlapping images
                                if len(cell_str.strip()) <= 2 and cell_bboxes:
                                    bbox = cell_bboxes.get((row_idx, col_idx))
                                    if bbox:
                                        img_path = _render_cell_image(
                                            page, bbox, page_images,
                                            page_num, table_idx, row_idx, col_idx,
                                            fig_dir
                                        )
                                        if img_path:
                                            # Store as special marker for renderer
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
        import io
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

        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        with open(fpath, "wb") as f:
            f.write(buf.getvalue())

        return fpath

    except Exception as e:
        log.debug("  Cell image render failed p%d t%d r%d c%d: %s",
                  page_num, table_idx, row_idx, col_idx, e)
        return ""