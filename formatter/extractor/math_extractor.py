"""
extractor/math_extractor.py — Stage 1 add-on: Math Region Extraction via pix2tex.

PURPOSE
-------
The core text pipeline (PyMuPDF → normalizer) can handle simple Greek letters
and operators, but fails on heavy calculus:
  - Multi-level integrals / summations with limits
  - Matrix / vector expressions
  - Fractions with complex numerators
  - Aligned equation blocks

pix2tex (LaTeX-OCR) solves this by treating each formula as an IMAGE and
doing image-to-LaTeX conversion. This module:
  1. Uses PyMuPDF to find formula bounding boxes on each page
  2. Crops those regions to PNG files
  3. Runs pix2tex on each crop
  4. Returns structured FormulaBlock objects

HOW IT FITS IN THE PIPELINE
-----------------------------
Called from pdf_extractor.py at the end of Stage 1.
The result list is stored in the rich dict under "formula_blocks".
pipeline.py passes it to the Document constructor.
The renderer reads doc.formula_blocks and inserts them as equation environments.

GRACEFUL DEGRADATION
--------------------
If pix2tex is not installed, this module logs a warning and returns [].
The rest of the pipeline continues unchanged.
Install with: pip install pix2tex[gui]
              (or just: pip install pix2tex)

DETECTION STRATEGY
------------------
We use PyMuPDF's block analysis.  A block is flagged as a formula region if:
  - It contains ≥ MIN_MATH_CHARS known math Unicode characters (∫ ∑ ∂ ∇ etc.)
  - OR it is a line that matches the equation-number pattern "(N)" at the end
  - OR its font name contains "Math", "Symbol", or "CMEX" / "CMSY" (TeX fonts)

Blocks that pass are cropped via the PyMuPDF page.get_pixmap(clip=bbox) API,
which renders the exact page region at HIGH_DPI resolution.

CONFIDENCE FILTERING
--------------------
pix2tex returns a confidence score per prediction.
We discard results below CONFIDENCE_THRESHOLD (default 0.45).
Low-confidence results are logged as warnings but not raised as errors.
"""

import logging
import os
import re

log = logging.getLogger(__name__)

# ── Tuneable constants ────────────────────────────────────────────────────────

# Minimum number of math Unicode characters in a block to flag it.
MIN_MATH_CHARS = 2

# DPI for the page-crop render.  150 is fast; 300 is better for small formulas.
HIGH_DPI = 200

# Drop pix2tex results below this confidence.
CONFIDENCE_THRESHOLD = 0.45

# Known math Unicode ranges / chars (extend as needed).
_MATH_CHARS = frozenset(
    "∫∬∭∮∂∇∆∏∑√∛∜∞≈≠≤≥≪≫≡≢∈∉⊂⊃⊆⊇∩∪∧∨⊕⊗⊥∥"
    "αβγδεζηθικλμνξπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ"
    "±×÷·⋅⌈⌉⌊⌋‖‗→←↔⇒⇐⇔·°′″"
)

# PyMuPDF font name substrings that indicate a math font.
_MATH_FONT_HINTS = ("math", "symbol", "cmex", "cmsy", "cmmi", "stix", "euler")

# Equation-number pattern at end of a text block, e.g. "(3)" or "(12)".
_EQ_NUM_PATTERN = re.compile(r"\(\d{1,3}\)\s*$")


# ── Public entry point ────────────────────────────────────────────────────────

def extract_formula_blocks(pdf_path: str, inter_dir: str) -> list:
    """
    Detect formula regions in the PDF, crop them, run pix2tex.

    Returns a list of dicts (one per formula):
        {
          "latex":      str,    # pix2tex result
          "image_path": str,    # path to saved PNG crop
          "page":       int,    # 1-based page number
          "confidence": float,  # pix2tex confidence
          "label":      str,    # "eq:1", "eq:2", ...
        }

    Returns [] if pix2tex is not installed or no formulas are detected.
    """
    # ── Guard: pix2tex must be installed ────────────────────────────────────
    LatexOCR = _try_import_pix2tex()
    if LatexOCR is None:
        log.warning(
            "[math_extractor] pix2tex not installed — skipping formula extraction. "
            "Install with: pip install pix2tex"
        )
        return []

    # ── Guard: fitz (PyMuPDF) must be available ──────────────────────────────
    try:
        import fitz  # noqa: F401
    except ImportError:
        log.warning("[math_extractor] PyMuPDF (fitz) not available — cannot crop formulas.")
        return []

    import fitz
    from PIL import Image
    import io

    formula_dir = os.path.join(inter_dir, "formula_crops")
    os.makedirs(formula_dir, exist_ok=True)

    log.info("[math_extractor] Scanning PDF for formula regions: %s", pdf_path)

    # Initialise pix2tex model once (it's slow to load).
    try:
        model = LatexOCR()
    except Exception as exc:
        log.error("[math_extractor] Failed to initialise pix2tex model: %s", exc)
        return []

    results = []
    eq_counter = 0

    doc = fitz.open(pdf_path)

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # Collect candidate bounding boxes on this page.
        candidates = _find_formula_bboxes(page)

        for bbox in candidates:
            # Render the bounding-box region at HIGH_DPI.
            try:
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(HIGH_DPI / 72, HIGH_DPI / 72),
                    clip=fitz.Rect(bbox),
                    colorspace=fitz.csGRAY,   # greyscale — pix2tex prefers it
                )
                img_bytes = pix.tobytes("png")
                pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            except Exception as exc:
                log.warning(
                    "[math_extractor] Crop failed on page %d bbox %s: %s",
                    page_num, bbox, exc
                )
                continue

            # Run pix2tex.
            try:
                latex, confidence = _run_pix2tex(model, pil_img)
            except Exception as exc:
                log.warning(
                    "[math_extractor] pix2tex failed on page %d: %s", page_num, exc
                )
                continue

            if confidence < CONFIDENCE_THRESHOLD:
                log.debug(
                    "[math_extractor] Low confidence %.2f on page %d — skipped.",
                    confidence, page_num
                )
                continue

            # Save the crop for debugging.
            eq_counter += 1
            label = f"eq:{eq_counter}"
            crop_filename = f"formula_{eq_counter}_p{page_num}.png"
            crop_path = os.path.join(formula_dir, crop_filename)
            pil_img.save(crop_path)

            results.append({
                "latex":      latex,
                "image_path": crop_path,
                "page":       page_num,
                "confidence": round(confidence, 3),
                "label":      label,
            })
            log.debug(
                "[math_extractor] eq:%d page %d conf=%.2f  latex=%s",
                eq_counter, page_num, confidence, latex[:60]
            )

    doc.close()
    log.info("[math_extractor] Extracted %d formula blocks.", len(results))
    return results


# ── Internal helpers ──────────────────────────────────────────────────────────

def _try_import_pix2tex():
    """
    Attempt to import pix2tex's LatexOCR class.
    Returns the class if available, None otherwise.
    This keeps the whole module safe to import even without pix2tex installed.
    """
    try:
        from pix2tex.cli import LatexOCR
        return LatexOCR
    except ImportError:
        return None


def _find_formula_bboxes(page) -> list:
    """
    Return a list of (x0, y0, x1, y1) bounding boxes for formula regions
    on this PyMuPDF page.

    Strategy:
    1. Iterate over text blocks via page.get_text("dict").
    2. Flag a block as a formula if it passes any of 3 tests:
       a) Contains >= MIN_MATH_CHARS known math Unicode characters.
       b) Ends with an equation number pattern like "(3)".
       c) Any span uses a known math font (CMR, STIX, etc.).
    3. Add a small padding margin around each detected bbox.
    4. Merge overlapping / adjacent boxes (formulas often span 2 blocks).
    """
    raw_data = page.get_text("dict", flags=0)  # flags=0 → include font info
    page_rect = page.rect
    candidates = []

    for block in raw_data.get("blocks", []):
        if block.get("type") != 0:   # type 0 = text; type 1 = image
            continue

        block_text = ""
        uses_math_font = False

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                block_text += span.get("text", "")
                font_name = span.get("font", "").lower()
                if any(hint in font_name for hint in _MATH_FONT_HINTS):
                    uses_math_font = True

        # Test a: math characters.
        math_char_count = sum(1 for ch in block_text if ch in _MATH_CHARS)

        # Test b: equation number at end.
        has_eq_num = bool(_EQ_NUM_PATTERN.search(block_text.strip()))

        if math_char_count >= MIN_MATH_CHARS or has_eq_num or uses_math_font:
            bbox = block["bbox"]   # (x0, y0, x1, y1)
            padded = _pad_bbox(bbox, padding=4, page_rect=page_rect)
            candidates.append(padded)

    # Merge overlapping candidates so a two-line formula is one crop.
    return _merge_overlapping(candidates)


def _pad_bbox(bbox: tuple, padding: int, page_rect) -> tuple:
    """Add a small padding margin and clamp to page bounds."""
    x0, y0, x1, y1 = bbox
    x0 = max(page_rect.x0, x0 - padding)
    y0 = max(page_rect.y0, y0 - padding)
    x1 = min(page_rect.x1, x1 + padding)
    y1 = min(page_rect.y1, y1 + padding)
    return (x0, y0, x1, y1)


def _merge_overlapping(boxes: list) -> list:
    """
    Merge bounding boxes that overlap or are within 8px of each other vertically.
    This joins multi-line formulas that PyMuPDF splits into separate blocks.
    """
    if not boxes:
        return []

    # Sort top-to-bottom.
    boxes = sorted(boxes, key=lambda b: b[1])
    merged = [boxes[0]]

    for current in boxes[1:]:
        prev = merged[-1]
        # Same x-column AND close vertically → merge.
        horizontal_overlap = prev[0] < current[2] and current[0] < prev[2]
        vertical_gap = current[1] - prev[3]

        if horizontal_overlap and vertical_gap <= 8:
            # Expand prev to include current.
            merged[-1] = (
                min(prev[0], current[0]),
                min(prev[1], current[1]),
                max(prev[2], current[2]),
                max(prev[3], current[3]),
            )
        else:
            merged.append(current)

    return merged


def _run_pix2tex(model, pil_image) -> tuple:
    """
    Run pix2tex on a PIL image.
    Returns (latex_string, confidence_float).

    pix2tex API varies slightly between versions:
      - Older (<=0.1.x): model(image) returns a string only.
      - Newer (>=0.1.2): model(image) returns (latex, confidence).
    We handle both.
    """
    result = model(pil_image)

    if isinstance(result, tuple):
        latex, confidence = result
    else:
        # Older API — no confidence available.
        latex = result
        confidence = 1.0   # assume good; user can lower CONFIDENCE_THRESHOLD

    latex = latex.strip()

    # Strip outer $...$ or $$...$$ that pix2tex sometimes adds.
    # The renderer will wrap in the correct equation environment.
    if latex.startswith("$$") and latex.endswith("$$"):
        latex = latex[2:-2].strip()
    elif latex.startswith("$") and latex.endswith("$"):
        latex = latex[1:-1].strip()

    return latex, float(confidence)