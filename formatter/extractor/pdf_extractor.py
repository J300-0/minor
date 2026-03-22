"""
extractor/pdf_extractor.py — Stage 1: PDF -> raw text + font-aware blocks + tables + images.

RULE: PyMuPDF (fitz) is the PRIMARY text extractor.
      pdfplumber is used ONLY for table detection and image extraction.
      Springer PDFs use CID/Adobe-Identity-UCS encoding that causes
      pdfplumber to silently return garbled text or nothing.

OCR:  pix2tex (LatexOCR) is the PRIMARY equation OCR — outputs LaTeX directly.
      Tesseract is the FALLBACK if pix2tex is not installed.
      Install: pip install pix2tex
"""
import os
import re
from extractor.math_extractor import extract_formuls_blocks 
from core.logger import get_logger

log = get_logger(__name__) 

# ── Equation OCR (pix2tex → nougat → Tesseract, all subprocess) ──────────────
#
# Both pix2tex and nougat run in SUBPROCESSES via worker scripts — never
# in-process.  Reason: LatexOCR() / NougatModel init can segfault or
# sys.exit() (CUDA DLL issues on Windows) and kill the pipeline.
#
# Fallback chain:  pix2tex → nougat (Meta) → Tesseract

_EXTRACTOR_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER = os.path.join(_EXTRACTOR_DIR, "pix2tex_worker.py")
_NOUGAT_WORKER = os.path.join(_EXTRACTOR_DIR, "nougat_worker.py")

# Cached availability flags (None = not yet checked)
_pix2tex_ok = None
_nougat_ok = None
_tesseract_ok = None


def _pix2tex_available() -> bool:
    """Check once whether pix2tex can be used (via subprocess worker)."""
    global _pix2tex_ok
    if _pix2tex_ok is not None:
        return _pix2tex_ok

    if not os.path.exists(_WORKER):
        log.warning("pix2tex_worker.py not found at %s", _WORKER)
        _pix2tex_ok = False
        return False

    import subprocess
    import sys

    # Two-stage check:
    # Stage 1: lightweight — just confirm pix2tex package is importable
    # Stage 2: confirm the worker script itself can run (catches model/weight issues)
    try:
        r1 = subprocess.run(
            [sys.executable, "-c", "import pix2tex"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if r1.returncode != 0:
            err = r1.stderr.decode(errors="replace").strip()
            log.info("pix2tex package not importable: %s", err[:200])
            _pix2tex_ok = False
            return False
    except Exception as e:
        log.info("pix2tex check failed: %s", e)
        _pix2tex_ok = False
        return False

    # Stage 2: confirm LatexOCR class is accessible (heavier, longer timeout)
    try:
        r2 = subprocess.run(
            [sys.executable, "-c", "from pix2tex.cli import LatexOCR; print('ok')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if r2.returncode == 0 and b"ok" in r2.stdout:
            _pix2tex_ok = True
        else:
            err = r2.stderr.decode(errors="replace").strip()
            log.info("pix2tex LatexOCR not available: %s", err[:200])
            _pix2tex_ok = False
    except Exception as e:
        log.info("pix2tex LatexOCR check timed out or failed: %s", e)
        _pix2tex_ok = False

    if _pix2tex_ok:
        log.info("pix2tex available (subprocess mode) — OCR'ing equation images")
    else:
        log.info("pix2tex not usable — falling back to Tesseract for image formulas")
    return _pix2tex_ok


def _nougat_available() -> bool:
    """Check once whether nougat (Meta) can be used via subprocess worker."""
    global _nougat_ok
    if _nougat_ok is not None:
        return _nougat_ok

    if not os.path.exists(_NOUGAT_WORKER):
        _nougat_ok = False
        return False

    import subprocess
    import sys
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import nougat"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        _nougat_ok = (r.returncode == 0)
    except Exception:
        _nougat_ok = False

    if _nougat_ok:
        log.info("nougat (Meta) available as fallback OCR")
    return _nougat_ok


def _tesseract_available() -> bool:
    """Check once whether Tesseract OCR is available."""
    global _tesseract_ok
    if _tesseract_ok is not None:
        return _tesseract_ok
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        _tesseract_ok = True
    except Exception:
        _tesseract_ok = False
    return _tesseract_ok


def _ocr_available() -> bool:
    """True if any OCR engine is available."""
    return _pix2tex_available() or _nougat_available() or _tesseract_available()


def _fix_array_col_spec(latex: str) -> str:
    """Fix \begin{array}{col_spec} when the declared column count doesn't
    match the actual number of & separators in the rows.

    pix2tex sometimes undercounts columns (e.g. writes {c c c c} for a
    5-column matrix), which causes a fatal 'Extra alignment tab' LaTeX error.

    Strategy: for each array block, count the max & per row → that is the
    real column count. If it differs from what the spec declares, rebuild
    the spec with that many 'c' columns (preserving any leading/trailing |).
    """
    def _count_spec_cols(spec: str) -> int:
        # Only c, l, r, p{...} count as real columns — not | or @{}
        return len(re.findall(r'[clrp]', spec))

    def fix_array(m):
        col_spec = m.group(1)
        body     = m.group(2)
        declared = _count_spec_cols(col_spec)

        # Split rows on \\ (the LaTeX row separator)
        rows = re.split(r'\\\\', body)
        max_cols = 0
        for row in rows:
            row_clean = row.strip()
            if not row_clean:
                continue
            # Count & in this row → columns = & + 1
            max_cols = max(max_cols, row_clean.count('&') + 1)

        if max_cols <= 1 or max_cols == declared:
            return m.group(0)  # nothing to fix

        # Rebuild spec: keep leading/trailing | borders, replace column chars
        leading  = re.match(r'^(\|*)', col_spec).group(1)
        trailing = re.search(r'(\|*)$', col_spec).group(1)
        new_spec = leading + ' '.join(['c'] * max_cols) + trailing
        log.debug("array col fix: {%s} → {%s} (%d→%d cols)",
                  col_spec, new_spec, declared, max_cols)
        return r'\begin{array}{' + new_spec + '}' + body + r'\end{array}'

    pattern = re.compile(
        r'\\begin\{array\}\{([^}]*)\}(.*?)\\end\{array\}',
        re.DOTALL,
    )
    return pattern.sub(fix_array, latex)


def _fix_unbalanced_braces(latex: str) -> str:
    """Append or prepend missing curly braces.

    pix2tex often emits {{{\Big|}}} patterns where nesting levels get
    miscounted, leaving unclosed { groups that cause 'Missing } inserted'.

    This does a simple depth walk to find the imbalance and patch it.
    """
    depth = 0
    min_depth = 0
    for ch in latex:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            min_depth = min(min_depth, depth)

    # min_depth < 0 means too many } — prepend that many {
    if min_depth < 0:
        latex = '{' * (-min_depth) + latex
        log.debug("brace fix: prepended %d '{'", -min_depth)

    # Recount after prepend fix, then append any remaining opens
    depth = 0
    for ch in latex:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1

    if depth > 0:
        latex = latex + '}' * depth
        log.debug("brace fix: appended %d '}'", depth)

    return latex


def _fix_unbalanced_delimiters(latex: str) -> str:
    r"""Balance \left and \right delimiters.

    pix2tex often emits stacked \left. without matching \right. (or vice versa),
    causing 'Extra }, or forgotten \right.' fatal errors in pdflatex.

    Strategy: count \left* and \right* occurrences.  If they don't match,
    append \right. or prepend \left. as needed — invisible delimiters that
    satisfy pdflatex's pairing requirement without changing appearance.
    """
    left_count  = len(re.findall(r'\\left[\.\(\[\{|]', latex))
    right_count = len(re.findall(r'\\right[\.\)\]\}|]', latex))

    if left_count > right_count:
        # Too many \left — append invisible \right. for each extra
        latex = latex + (r'\right.' * (left_count - right_count))
        log.debug("delimiter fix: added %d \\right.", left_count - right_count)
    elif right_count > left_count:
        # Too many \right — prepend invisible \left.
        latex = (r'\left.' * (right_count - left_count)) + latex
        log.debug("delimiter fix: added %d \\left.", right_count - left_count)

    return latex


def _is_latex_safe(latex: str) -> bool:
    """Return True if the LaTeX string has balanced braces and delimiters."""
    # Check curly braces
    depth = 0
    for ch in latex:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                return False
    if depth != 0:
        return False
    # Check \left / \right balance
    lc = len(re.findall(r'\\left[\.\(\[\{|]', latex))
    rc = len(re.findall(r'\\right[\.\)\]\}|]', latex))
    return lc == rc


def _sanitize_ocr_latex(latex: str) -> str:
    """Clean up common pix2tex output issues that cause pdflatex errors.

    Applied to every pix2tex result before it enters the pipeline.
    Returns empty string if the result cannot be made safe.
    """
    # Fix array column count mismatches (Fatal: Extra alignment tab)
    latex = _fix_array_col_spec(latex)
    # Fix unbalanced { } (Fatal: Missing } inserted)
    latex = _fix_unbalanced_braces(latex)
    # Balance \left / \right pairs (Fatal: Extra }, or forgotten \right.)
    latex = _fix_unbalanced_delimiters(latex)

    # Final gate: if still broken, reject rather than crash pdflatex
    if not _is_latex_safe(latex):
        log.warning("OCR LaTeX still unsafe after fixes — discarding: %s", latex[:80])
        return ""

    return latex


def _run_worker(worker_path: str, image_path: str, name: str,
                timeout: int = 60) -> str:
    """Run an OCR worker subprocess and return its stdout or ''."""
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, worker_path, image_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            log.debug("%s worker: %s", name, result.stderr.strip()[:120])
    except Exception as e:
        log.debug("%s subprocess failed: %s", name, e)
    return ""


def _ocr_equation(image_path: str) -> tuple:
    """OCR an equation image file → (latex_str, source).

    Fallback chain: pix2tex → nougat → Tesseract.
    All ML models run in subprocesses to isolate crashes.

    Returns:
      (latex_str, "pix2tex")   — LaTeX from pix2tex
      (latex_str, "nougat")    — LaTeX from nougat (Meta)
      (text_str,  "tesseract") — plain text from Tesseract
      ("", None)               — all failed
    """
    if not image_path or not os.path.exists(image_path):
        return "", None

    # ── 1. pix2tex (fastest, direct LaTeX output) ─────────────────────────
    if _pix2tex_available():
        raw = _run_worker(_WORKER, image_path, "pix2tex")
        if raw:
            latex = _sanitize_ocr_latex(raw)
            if latex:
                log.debug("pix2tex [subprocess]: %s", latex[:80])
                return latex, "pix2tex"

    # ── 2. nougat / Meta (designed for scientific documents) ──────────────
    if _nougat_available():
        raw = _run_worker(_NOUGAT_WORKER, image_path, "nougat", timeout=90)
        if raw:
            latex = _sanitize_ocr_latex(raw)
            if latex:
                log.debug("nougat [subprocess]: %s", latex[:80])
                return latex, "nougat"

    # ── 3. Tesseract (plain text fallback) ────────────────────────────────
    if _tesseract_available():
        try:
            from PIL import Image
            pil_img = Image.open(image_path)
            text = _tesseract_multiconfig(pil_img)
            if text:
                return text, "tesseract"
        except Exception as e:
            log.debug("Tesseract failed: %s", e)

    return "", None


def _is_valid_equation_ocr(text: str, source: str) -> bool:
    """Check if OCR output looks like a real equation.

    pix2tex output is trusted more (it only outputs LaTeX).
    Tesseract output needs stricter validation.
    """
    if not text or len(text) < 2:
        return False

    # pix2tex and nougat output LaTeX — trust if any math content
    if source in ("pix2tex", "nougat"):
        # Reject if it's just a number or single letter
        if re.match(r"^[a-zA-Z0-9]$", text.strip()):
            return False
        # Sometimes outputs empty braces or noise
        clean = re.sub(r"[{}\s\\]", "", text)
        return len(clean) >= 2

    # Tesseract: stricter check — must contain math-like patterns
    # AND not look like garbled OCR noise from image formulas
    math_indicators = (
        "=" in text
        or "+" in text
        or ("-" in text and len(text) > 5)
        or "∫" in text or "∑" in text or "∏" in text
        or "lim" in text.lower()
        or "log" in text.lower()
        or "sin" in text.lower() or "cos" in text.lower()
        or re.search(r"\b[a-z]\s*[=<>≤≥]\s*", text)
        or re.search(r"\bd[a-z]/d[a-z]", text)
        or re.search(r"[²³⁴⁵⁶⁷⁸⁹]", text)
        or re.search(r"\^", text)
    )
    alpha_count = sum(1 for c in text if c.isalnum() or c in "=+-*/()[]{}^_.,<>≤≥∫∑")
    if len(text) > 0 and alpha_count / len(text) < 0.3:
        return False

    # Reject garbled Tesseract output: multi-line, excessive uppercase,
    # or too many non-math special chars (signs of failed math OCR)
    if "\n" in text and text.count("\n") >= 2:
        return False  # real equations rarely span 3+ lines in OCR
    upper_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
    if upper_ratio > 0.5 and len(text) > 10:
        return False  # garbled caps like "HEF", "Jj HEF"
    # Reject if it has too many word-like tokens (Tesseract reading text, not math)
    words = text.split()
    long_words = [w for w in words if len(w) > 3 and w.isalpha()]
    if len(long_words) >= 3:
        return False  # e.g. "Pare fern den" — Tesseract reading image as text

    return math_indicators


def _wrap_latex(text: str, source: str) -> str:
    """Wrap OCR output in $...$ for inline LaTeX rendering.

    pix2tex output is already LaTeX — just wrap in $.
    Tesseract output is plain text — return as-is (cleaner handles it).
    """
    if source in ("pix2tex", "nougat"):
        # pix2tex/nougat return raw LaTeX like "\\frac{df}{dx}" — wrap in $...$
        text = text.strip()
        # Don't double-wrap if already has $
        if text.startswith("$"):
            return text
        return f"${text}$"
    # Tesseract: return raw text, let normalizer/cleaner handle it
    return text


def _filter_table_text_from_blocks(blocks: list, tables: list) -> list:
    """Remove text blocks whose content appears in table cells.

    When pdfplumber extracts a table, the same text also appears in the
    text blocks (body text).  This causes duplicated content — table rows
    show up as section body text AND as table data.  We detect this by
    checking if a block's text is a substring of any table cell content
    on the same page.
    """
    if not tables:
        return blocks

    # Build per-page set of table cell strings (lowered, 10+ chars)
    table_strings = {}
    for tbl in tables:
        pg = tbl.get("page", 0)
        if pg not in table_strings:
            table_strings[pg] = set()
        for row in [tbl.get("headers", [])] + tbl.get("rows", []):
            for cell in row:
                cell_s = str(cell or "").strip().lower()
                if len(cell_s) >= 10:
                    table_strings[pg].add(cell_s)

    filtered = []
    removed = 0
    for b in blocks:
        pg = b.get("page", 0)
        txt = b["text"].strip().lower()
        if pg in table_strings and len(txt) >= 10:
            # Check if this block's text matches or is contained in a table cell
            if any(txt in cell_s or cell_s in txt
                   for cell_s in table_strings[pg]):
                removed += 1
                continue
        filtered.append(b)

    if removed:
        log.info("Filtered %d text blocks that duplicated table content", removed)
    return filtered


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
    tables   = _extract_tables(pdf_path)
    # Filter table text from body blocks to avoid duplication
    blocks   = _filter_table_text_from_blocks(blocks, tables)
    raw_text = _blocks_to_text(blocks)
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
        "formula_blocks": formula_blocks, 
    }


# ══════════════════════════════════════════════════════════════════════════════
# Block extraction — PyMuPDF primary, pdfplumber fallback
# ══════════════════════════════════════════════════════════════════════════════

def _extract_blocks(pdf_path: str) -> list:
    """Extract text blocks with font size and bold flag using PyMuPDF.

    Image blocks (type=1) are OCR'd to recover inline equations:
      - pix2tex (primary): outputs LaTeX directly (e.g. "\\frac{df}{dx}")
      - Tesseract (fallback): outputs plain text
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        log.warning("pymupdf not installed — falling back to pdfplumber line blocks")
        return _fallback_blocks(pdf_path)

    use_ocr = _ocr_available()
    if use_ocr:
        ocr_name = "pix2tex (subprocess)" if _pix2tex_available() else "Tesseract"
        log.info("%s available — will OCR inline equation images", ocr_name)

    blocks = []
    ocr_count = 0
    doc = fitz.open(pdf_path)

    for page_num, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in page_dict.get("blocks", []):
            if block.get("type") == 0:  # text block
                lines_text = []
                font_sizes = []
                is_bold = False
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        t = span.get("text", "").strip()
                        if t:
                            lines_text.append(t)
                            font_sizes.append(span.get("size", 10))
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

            elif block.get("type") == 1 and use_ocr:  # image block
                # OCR inline images to recover equation text
                try:
                    w = block.get("width", 0)
                    h = block.get("height", 0)
                    # Skip tiny (icons/rules) and huge (photos/figures)
                    if w < 50 or h < 15 or w > 2000 or h > 1500:
                        continue
                    img_data = block.get("image")
                    if not img_data:
                        continue
                    from PIL import Image
                    import io
                    import tempfile
                    pil_img = Image.open(io.BytesIO(img_data))
                    with tempfile.NamedTemporaryFile(
                            suffix=".png", delete=False) as tmp:
                        pil_img.save(tmp.name)
                        tmp_path = tmp.name
                    ocr_text, ocr_src = _ocr_equation(image_path=tmp_path)
                    os.unlink(tmp_path)
                    if ocr_text and _is_valid_equation_ocr(ocr_text, ocr_src):
                        wrapped = _wrap_latex(ocr_text, ocr_src)
                        ocr_count += 1
                        blocks.append({
                            "text":       wrapped,
                            "font_size":  10,
                            "bold":       False,
                            "page":       page_num,
                            "ocr":        True,
                            "ocr_source": ocr_src,
                        })
                        log.debug("OCR [%s] equation p%d: %s",
                                  ocr_src, page_num, wrapped[:80])
                except Exception as e:
                    log.debug("OCR block extraction failed: %s", e)

    doc.close()
    if ocr_count:
        log.info("OCR recovered %d inline equation(s)", ocr_count)
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

def _is_clean_cell_ocr(text: str, source: str, lax: bool = False) -> bool:
    """Extra quality gate for table cell OCR results.

    Tesseract is poor at reading complex math from images.
    pix2tex/nougat output LaTeX directly — always trusted.

    lax=True is used for cells where an embedded image was detected.
    In lax mode we require fewer guarantees but still filter obvious garbage.
    """
    if source in ("pix2tex", "nougat"):
        return True  # LaTeX OCR engines — trust their output

    # Common checks
    if not text or len(text.strip()) < 3:
        return False
    if text.count("\n") >= 2:
        return False  # Multi-line = likely garbled block output

    # Count meaningful (non-whitespace) characters
    meaningful = re.sub(r'\s+', '', text)

    if lax:
        # Lax mode: we know there's an embedded image formula in this cell.
        # Accept if: no newline (table cells must be single-line), enough
        # chars (min 7 filters "B= fmt"=5, "b= Gaye"=6), and some math char.
        return (
            "\n" not in text                          # no newlines — would break table row
            and len(meaningful) >= 7                  # filter short garbled outputs
            and bool(re.search(r'[=+\-\(\)\[\]\{\}0-9\/∫∑∏√]', text))
        )

    # Strict mode: require classic formula structure
    if len(meaningful) < 8:
        return False
    # Reject trivial "X = word" patterns (single var = short word — garbled OCR)
    if re.match(r'^[a-zA-Z]{1,2}\s*=\s*[a-zA-Z]{2,8}\s*$', text.strip()):
        return False
    # Reject if too many pure-alpha English-looking words
    words = text.split()
    alpha_words = [w for w in words if w.isalpha() and len(w) > 2]
    if len(alpha_words) >= 2:
        return False  # e.g. "Pare", "fern", "den" — not math
    # Must have a recognizable math structure: var = expr
    if not re.search(r'[a-zA-Z]\s*[=<>]\s*\S', text):
        return False
    return True


def _score_ocr_text(text: str) -> float:
    """Score a Tesseract result for math-like content (higher = more formula-like).

    Scoring heuristic:
      +3  contains '='
      +2  contains math operator (+-×÷∫∑∏√)
      +2  letter directly followed by = or < or > (e.g. "F = G")
      +2  contains trig/log/lim function name
      +2  no embedded newlines (single-line = formula-like)
      +1  contains digit
      -N  penalised by length/30 (prefer concise over long garbage)
    """
    score = 0.0
    if "=" in text:
        score += 3
    if any(c in text for c in "+-×÷∫∑∏√"):
        score += 2
    if re.search(r"[a-zA-Z]\s*[=<>]", text):
        score += 2
    if re.search(r"\bsin\b|\bcos\b|\btan\b|\blog\b|\blim\b", text, re.I):
        score += 2
    if "\n" not in text:
        score += 2
    if re.search(r"\d", text):
        score += 1
    score -= len(text) / 30
    return score


def _tesseract_all_configs(pil_img) -> list:
    """Run Tesseract with multiple PSM configs; return all results sorted by score desc.

    Returns list of text strings (not tuples) for easy iteration.
    Empty strings and duplicates are removed.
    """
    try:
        import pytesseract
        configs = [
            "--psm 7 --oem 1",   # single text line, LSTM (best for formulas)
            "--psm 8 --oem 1",   # single word, LSTM
            "--psm 6 --oem 1",   # uniform block, LSTM
            "--psm 11 --oem 1",  # sparse text, LSTM
        ]
        seen = set()
        candidates = []
        for cfg in configs:
            try:
                text = pytesseract.image_to_string(pil_img, config=cfg).strip()
                if text and text not in seen:
                    seen.add(text)
                    candidates.append((_score_ocr_text(text), text))
            except Exception:
                pass
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in candidates]
    except Exception:
        return []


def _tesseract_multiconfig(pil_img) -> str:
    """Return the highest-scoring Tesseract result (for non-cell equation OCR)."""
    results = _tesseract_all_configs(pil_img)
    return results[0] if results else ""


def _ocr_cell(page, bbox, page_images=None) -> str:
    """OCR a single table cell region and return LaTeX string or ''.

    If page_images is provided, looks for an embedded image whose bbox
    overlaps the cell and uses that tighter crop for OCR.  This removes
    cell borders and whitespace padding that confuse OCR on formula images.

    Fallback: crops the full cell bbox at 400 DPI with preprocessing.
    """
    import tempfile
    try:
        x0, top, x1, bottom = bbox
        cell_w = x1 - x0
        cell_h = bottom - top
        if cell_w < 10 or cell_h < 5:
            return ""

        # ── 1. Find tightest crop from embedded image bbox ─────────────────
        # pdfplumber page.images entries have keys: x0, top, x1, bottom
        ocr_bbox = None
        if page_images:
            best_overlap = 0
            for img in page_images:
                ix0    = img.get("x0", 0)
                itop   = img.get("top", 0)
                ix1    = img.get("x1", 0)
                ibottom = img.get("bottom", 0)
                # Intersection with cell bbox
                inter_x0     = max(x0, ix0)
                inter_top    = max(top, itop)
                inter_x1     = min(x1, ix1)
                inter_bottom = min(bottom, ibottom)
                if inter_x1 > inter_x0 and inter_bottom > inter_top:
                    overlap = (inter_x1 - inter_x0) * (inter_bottom - inter_top)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        # Clamp image bbox to cell bbox
                        ocr_bbox = (
                            max(ix0, x0), max(itop, top),
                            min(ix1, x1), min(ibottom, bottom),
                        )

        if ocr_bbox:
            log.debug("Cell OCR: using embedded image bbox %s (cell %s)",
                      ocr_bbox, bbox)

        # Use tighter image bbox if found, otherwise full cell bbox
        crop_bbox = ocr_bbox if ocr_bbox else bbox
        cx0, ctop, cx1, cbottom = crop_bbox

        # Inset by 2pt to trim table borders that confuse OCR
        inset = 2.0
        final_crop = (
            min(cx0 + inset, cx1),
            min(ctop + inset, cbottom),
            max(cx1 - inset, cx0),
            max(cbottom - inset, ctop),
        )
        cropped = page.crop(final_crop)

        # Render at high DPI for sharp edges (400 DPI for better math detail)
        rendered = cropped.to_image(resolution=400)
        pil_img = rendered.annotated  # PIL Image from PageImage

        # Pre-process: grayscale + auto-contrast + binary threshold
        from PIL import Image as PILImage, ImageOps, ImageFilter
        gray = pil_img.convert("L")
        gray = ImageOps.autocontrast(gray, cutoff=5)
        gray = gray.filter(ImageFilter.SHARPEN)
        gray_sharp = gray  # preserve for lax fallback variant (bw128)
        # Binary threshold (180 = aggressive, removes gray anti-aliasing)
        bw = gray.point(lambda p: 255 if p > 180 else 0, "1")
        pil_img = bw.convert("RGB")

        # Add white padding (30% on each side) for proper OCR framing
        pad_x = max(int(pil_img.width * 0.3), 30)
        pad_y = max(int(pil_img.height * 0.3), 30)
        padded = PILImage.new(
            "RGB",
            (pil_img.width + 2 * pad_x, pil_img.height + 2 * pad_y),
            (255, 255, 255),
        )
        padded.paste(pil_img, (pad_x, pad_y))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            padded.save(tmp.name)
            tmp_path = tmp.name

        has_image = ocr_bbox is not None  # True when embedded image was found

        # ── Try pix2tex / nougat first (subprocesses) ─────────────────────
        ocr_text, ocr_src = _ocr_equation(image_path=tmp_path)
        if ocr_text and _is_valid_equation_ocr(ocr_text, ocr_src) \
                and _is_clean_cell_ocr(ocr_text, ocr_src):
            os.unlink(tmp_path)
            return _wrap_latex(ocr_text, ocr_src)

        # ── Tesseract lax fallback for cells with embedded images ──────────
        # The strict gate above already tried one Tesseract result.
        # For cells where we know an embedded image is present, iterate ALL
        # PSM configs AND two preprocessing variants (bw180 already saved,
        # plus softer bw128) and accept the first result that passes the
        # lax gate (no newline, 7+ meaningful chars, any math character).
        if has_image and _tesseract_available():
            try:
                from PIL import Image as _PILLoad
                # Variant 1: current preprocessed image (bw180, already saved)
                tess_img1 = _PILLoad.open(tmp_path)
                candidates1 = _tesseract_all_configs(tess_img1)

                # Variant 2: softer threshold (128 instead of 180) — better for
                # complex fractions where fine lines get erased by aggressive bw.
                # Uses gray_sharp (autocontrast+sharpen, before bw180 was applied).
                bw128 = gray_sharp.point(lambda p: 255 if p > 128 else 0, "1").convert("RGB")
                # Same padding as variant 1
                padded2 = PILImage.new(
                    "RGB",
                    (bw128.width + 2 * pad_x, bw128.height + 2 * pad_y),
                    (255, 255, 255),
                )
                padded2.paste(bw128, (pad_x, pad_y))
                candidates2 = _tesseract_all_configs(padded2)

                # Merge + re-sort by score (dedup)
                seen = set()
                merged = []
                for t in candidates1 + candidates2:
                    if t not in seen:
                        seen.add(t)
                        merged.append((_score_ocr_text(t), t))
                merged.sort(key=lambda x: x[0], reverse=True)
                all_candidates = [t for _, t in merged]

                for candidate in all_candidates:
                    if _is_clean_cell_ocr(candidate, "tesseract", lax=True):
                        log.debug("Cell OCR lax accept: %s", candidate[:60])
                        os.unlink(tmp_path)
                        return candidate  # plain text — no $ wrapping for Tesseract

                # Last resort: collapse multi-line results to a single line.
                # Multi-level formulas (e.g. df/dt = lim ...) produce multi-
                # newline OCR output that would break table rows.  Collapsing
                # can give something recognizable even if partially garbled.
                for candidate in all_candidates:
                    if "\n" in candidate:
                        collapsed = " ".join(candidate.split())
                        if _is_clean_cell_ocr(collapsed, "tesseract", lax=True):
                            log.debug("Cell OCR collapsed: %s", collapsed[:60])
                            os.unlink(tmp_path)
                            return collapsed
            except Exception as e2:
                log.debug("Lax Tesseract fallback failed: %s", e2)

        os.unlink(tmp_path)
    except Exception as e:
        log.debug("Cell OCR failed: %s", e)
    return ""


def _fitz_cell_text(fitz_doc, page_idx: int, bbox: tuple) -> str:
    """Extract text from a table cell region using PyMuPDF.

    PyMuPDF handles more font encodings than pdfplumber,
    especially for math-mode LaTeX-rendered text in PDFs.
    Returns cleaned text or empty string.
    """
    try:
        import fitz
        page = fitz_doc[page_idx]
        x0, top, x1, bottom = bbox
        rect = fitz.Rect(x0, top, x1, bottom)
        text = page.get_text("text", clip=rect).strip()
        if text:
            # Collapse internal newlines (cell text should be single-line)
            text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception:
        return ""


def _sort_cells_into_rows(cells_bbox: list) -> list:
    """Sort flat cell bbox list into row-major order.

    pdfplumber's Table.cells may NOT be in the same order as
    extract_tables() row data.  This sorts by (y-position, x-position)
    and groups cells into rows using a 5pt y-tolerance.

    Returns: list of lists — cell_rows[row][col] = (x0, top, x1, bottom).
    """
    if not cells_bbox:
        return []
    sorted_cells = sorted(cells_bbox, key=lambda c: (round(c[1], 0), c[0]))
    cell_rows = []
    current_row = [sorted_cells[0]]
    for cell in sorted_cells[1:]:
        if abs(cell[1] - current_row[0][1]) < 5:
            current_row.append(cell)
        else:
            cell_rows.append(sorted(current_row, key=lambda c: c[0]))
            current_row = [cell]
    cell_rows.append(sorted(current_row, key=lambda c: c[0]))
    return cell_rows


def _extract_tables(pdf_path: str) -> list:
    """Extract tables using pdfplumber for structure + PyMuPDF for cell text.

    For cells that come back empty from pdfplumber (formula rendered as image
    or vector math), tries two fallbacks:
      1. PyMuPDF text extraction (handles more font encodings)
      2. OCR via pix2tex / nougat / Tesseract (for raster image formulas)

    IMPORTANT: Table.cells bboxes are NOT guaranteed to be in the same order
    as extract_tables() data.  We sort them into row-major order by position.
    """
    tables = []
    use_ocr = _ocr_available()
    cell_ocr_count = 0
    cell_fitz_count = 0

    # Load PyMuPDF for cell text fallback (may not be installed)
    fitz_doc = None
    try:
        import fitz
        fitz_doc = fitz.open(pdf_path)
    except Exception:
        pass

    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                found  = page.find_tables()
                extracted = page.extract_tables() or []

                # Collect all image bboxes on this page once (for cell OCR)
                page_images = page.images or []

                for tbl_obj, tbl_data in zip(found, extracted):
                    if not tbl_data or len(tbl_data) < 2:
                        continue

                    n_cols = len(tbl_data[0]) if tbl_data else 0

                    # Sort cell bboxes into proper row-major order
                    cell_rows = _sort_cells_into_rows(tbl_obj.cells)

                    # Try to find table caption in text above the table
                    caption = ""
                    try:
                        tbl_top = tbl_obj.bbox[1]
                        cap_top = max(0, tbl_top - 60)
                        cap_region = page.crop(
                            (0, cap_top, page.width, tbl_top))
                        cap_text = cap_region.extract_text() or ""
                        for cap_line in cap_text.strip().split("\n"):
                            cap_line = cap_line.strip()
                            if re.match(r"(?i)^table\s+\d", cap_line):
                                caption = cap_line
                                break
                    except Exception:
                        pass

                    # Detect whether row 0 is a real header row.
                    # If the first cell is a plain number (e.g. "1"), there is
                    # no header row — all rows are data rows.
                    first_cell_0 = str(tbl_data[0][0] or "").strip()
                    has_header_row = (
                        bool(first_cell_0)
                        and not re.match(r"^\d+$", first_cell_0)
                    )

                    if has_header_row:
                        headers   = [str(c or "").strip() for c in tbl_data[0]]
                        data_rows = list(enumerate(tbl_data[1:], start=1))
                    else:
                        headers   = []
                        data_rows = list(enumerate(tbl_data, start=0))

                    rows = []
                    for row_idx, row in data_rows:
                        new_row = []
                        for col_idx, cell in enumerate(row):
                            cell_text = str(cell or "").strip()

                            # Get the correct bbox for this row/col
                            cell_bbox = None
                            if row_idx < len(cell_rows) and \
                               col_idx < len(cell_rows[row_idx]):
                                cell_bbox = cell_rows[row_idx][col_idx]

                            # Empty cell — try PyMuPDF text extraction first
                            if not cell_text and fitz_doc and cell_bbox:
                                fitz_text = _fitz_cell_text(
                                    fitz_doc, page_num - 1, cell_bbox)
                                if fitz_text:
                                    cell_text = fitz_text
                                    cell_fitz_count += 1
                                    log.debug(
                                        "Table cell fitz p%d r%d c%d: %s",
                                        page_num, row_idx, col_idx,
                                        cell_text[:60])

                            # Still empty — try OCR as last resort
                            if not cell_text and use_ocr and cell_bbox:
                                ocr_result = _ocr_cell(page, cell_bbox,
                                                       page_images=page_images)
                                if ocr_result:
                                    cell_text = ocr_result
                                    cell_ocr_count += 1
                                    log.debug(
                                        "Table cell OCR p%d r%d c%d: %s",
                                        page_num, row_idx, col_idx,
                                        cell_text[:60])
                            new_row.append(cell_text)
                        rows.append(new_row)

                    if rows:  # save table even when no header row
                        tables.append({
                            "caption": caption,
                            "headers": headers,
                            "rows":    rows,
                            "notes":   "",
                            "page":    page_num,
                        })

    except Exception as e:
        log.warning("Table extraction failed: %s", e)

    if fitz_doc:
        fitz_doc.close()

    log.info("Extracted %d tables from PDF (%d cells via fitz, %d via OCR)",
             len(tables), cell_fitz_count, cell_ocr_count)
    return tables


# ══════════════════════════════════════════════════════════════════════════════
# Image extraction — pdfplumber ONLY
# ══════════════════════════════════════════════════════════════════════════════

def _extract_images(pdf_path: str, inter_dir: str) -> list:
    """Extract images by cropping pdfplumber page regions.

    Runs equation OCR (pix2tex primary, Tesseract fallback) on each image.
    Images with recognized math are flagged as inline equations so the
    parser can inject their LaTeX text into the document flow.
    """
    images = []
    img_dir = os.path.join(inter_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    use_ocr = _ocr_available()

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
                        img_info = {
                            "path":     filepath,
                            "filename": filename,
                            "page":     page_num,
                            "width":    w,
                            "height":   h,
                        }
                        # OCR the image for equation recovery
                        if use_ocr:
                            ocr_text, ocr_src = _ocr_equation(
                                image_path=filepath)
                            if ocr_text and _is_valid_equation_ocr(
                                    ocr_text, ocr_src):
                                wrapped = _wrap_latex(ocr_text, ocr_src)
                                img_info["ocr_text"] = wrapped
                                img_info["ocr_source"] = ocr_src
                                img_info["is_equation"] = True
                                log.debug("OCR [%s] equation %s: %s",
                                          ocr_src, filename, wrapped[:80])
                        images.append(img_info)
                    except Exception as e:
                        log.warning("Failed to extract image %d from page %d: %s",
                                    img_idx, page_num, e)
    except Exception as e:
        log.warning("Image extraction failed: %s", e)

    ocr_eq_count = sum(1 for i in images if i.get("is_equation"))
    log.info("Extracted %d images from PDF (%d with OCR equations)",
             len(images), ocr_eq_count)
    return images
