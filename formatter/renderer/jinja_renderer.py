"""
renderer/jinja_renderer.py — Jinja2 with LaTeX-safe delimiters → .tex file.

Custom filters: latex_escape, latex_paragraphs (paras), render_table, section_cmd.
Copies required .cls files alongside generated .tex.
"""
import os
import re
import shutil
import logging
from dataclasses import asdict

import jinja2

from core.config import TEMPLATE_DIR, INTERMEDIATE_DIR, TEMPLATE_REGISTRY
from core.models import Document, Table
from core.shared import latex_relpath, OCR_RENDERER_THRESHOLD

log = logging.getLogger("paper_formatter")


def _is_valid_png(abs_path: str) -> bool:
    """Check that a PNG file can be read without error by PIL and pdflatex."""
    if not abs_path or not os.path.isfile(abs_path):
        return False
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            img.load()  # force full decode — catches truncated PNGs
        return True
    except Exception:
        return False


def render(doc: Document, template_name: str) -> str:
    """
    Render a Document into a .tex file using the specified template.
    Returns the path to the generated .tex file.
    """
    tmpl_info = TEMPLATE_REGISTRY[template_name]
    tmpl_dir = os.path.join(TEMPLATE_DIR, template_name)
    j2_path = os.path.join(tmpl_dir, tmpl_info["j2"])

    if not os.path.isfile(j2_path):
        raise FileNotFoundError(f"Template not found: {j2_path}")

    # Set up Jinja2 environment with LaTeX-safe delimiters
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(tmpl_dir),
        block_start_string=r"\BLOCK{",
        block_end_string=r"\ENDBLOCK",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        line_statement_prefix="%%",
        line_comment_prefix="%#",
        trim_blocks=True,
        autoescape=False,
    )

    # Register custom filters
    env.filters["e"] = _latex_escape
    env.filters["paras"] = _latex_paragraphs
    env.filters["render_table"] = _render_table
    env.filters["section_cmd"] = _section_cmd

    template = env.get_template(tmpl_info["j2"])

    # Build context from Document
    context = {
        "title": doc.title or "Untitled",
        "authors": [_author_to_dict(a) for a in doc.authors],
        "abstract": doc.abstract or "",
        "keywords": doc.keywords or [],
        "sections": [_section_to_dict(s) for s in doc.sections],
        "references": [_ref_to_dict(r) for r in doc.references],
        "formula_blocks": [_fb_to_dict(fb) for fb in doc.formula_blocks],
    }

    # Render
    tex_content = template.render(**context)

    # Write .tex file
    tex_path = os.path.join(INTERMEDIATE_DIR, "generated.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex_content)

    # Copy .cls file if needed
    if tmpl_info.get("cls"):
        cls_src = os.path.join(tmpl_dir, tmpl_info["cls"])
        cls_dst = os.path.join(INTERMEDIATE_DIR, tmpl_info["cls"])
        if os.path.isfile(cls_src):
            shutil.copy2(cls_src, cls_dst)
            log.debug("  Copied %s → %s", tmpl_info["cls"], INTERMEDIATE_DIR)

    return tex_path


# ── Context conversion helpers ───────────────────────────────────

def _author_to_dict(a):
    return {
        "name": a.name, "department": a.department,
        "organization": a.organization, "city": a.city,
        "country": a.country, "email": a.email,
    }

def _section_to_dict(s):
    # Build interleaved content blocks (text paragraphs + formulas in order)
    content_blocks = _build_content_blocks(s)
    return {
        "heading": s.heading, "depth": s.depth, "body": s.body,
        "content_blocks": content_blocks,
        "tables": s.tables,
        "figures": [_figure_to_dict(f) for f in s.figures],
        "formula_blocks": [_fb_to_dict(fb) for fb in s.formula_blocks],
    }


def _build_content_blocks(section) -> list:
    """
    Interleave text paragraphs and formula blocks by position.

    Uses body_positions (populated by the parser) to place each formula
    after the paragraph that immediately precedes it in the source PDF.
    Falls back to sequential append when positions are unavailable.

    Returns list of dicts:
      {"type": "text", "content": "escaped paragraph text"}
      {"type": "raw_latex", "content": "..."}
      {"type": "equation", "latex": "...", "image_path": "...", "label": "..."}
    """
    body = section.body or ""
    fbs = list(section.formula_blocks or [])

    if not fbs:
        # No formula block images — just process text paragraphs
        if body.strip():
            paragraphs = [p.strip() for p in re.split(r"\n\n+", body.strip()) if p.strip()]
            blocks = []
            for para in paragraphs:
                _split_para_blocks(blocks, para)
            return blocks if blocks else [{"type": "text", "content": _latex_paragraphs(body)}]
        return []

    # Split body into paragraphs
    paragraphs = [p.strip() for p in re.split(r"\n\n+", body.strip()) if p.strip()] if body.strip() else []

    # Sort formulas by position (page, then y)
    sorted_fbs = sorted(fbs, key=lambda fb: (fb.page, fb.bbox_y))

    if not paragraphs:
        return [_fb_to_content_block(fb) for fb in sorted_fbs]

    # Get paragraph positions from parser (one (page, y) per paragraph)
    positions = getattr(section, "body_positions", []) or []

    # Use position-aware placement when we have paragraph positions
    if positions and len(positions) == len(paragraphs):
        fb_after_para = _match_formulas_to_paragraphs(sorted_fbs, positions)
    else:
        # Fallback: no position data — place formulas sequentially at end
        # of the section (better than wrong interleaving)
        fb_after_para = {len(paragraphs) - 1: sorted_fbs}

    # Build interleaved blocks
    blocks = []
    for i, para in enumerate(paragraphs):
        _split_para_blocks(blocks, para)
        for fb in fb_after_para.get(i, []):
            blocks.append(_fb_to_content_block(fb))

    return blocks


def _match_formulas_to_paragraphs(sorted_fbs: list, positions: list) -> dict:
    """
    Match each formula to the paragraph it should appear after.

    Each formula has (page, bbox_y). Each paragraph has (page, y) from
    the parser. A formula is placed after the last paragraph whose
    position is <= the formula's position (reading order).

    Args:
        sorted_fbs: FormulaBlocks sorted by (page, bbox_y)
        positions: list of (page, y) tuples, one per paragraph

    Returns:
        dict mapping paragraph_index -> [formula_blocks to insert after it]
    """
    n_para = len(positions)
    fb_after_para = {}

    for fb in sorted_fbs:
        fb_pos = (fb.page, fb.bbox_y)

        # Find the last paragraph that comes before this formula
        best_idx = 0
        for i, (p_page, p_y) in enumerate(positions):
            if (p_page, p_y) <= fb_pos:
                best_idx = i
            else:
                # Paragraphs after the formula — stop searching
                break

        fb_after_para.setdefault(best_idx, []).append(fb)

    return fb_after_para


def _split_para_blocks(blocks: list, para: str):
    """
    Split a paragraph that may contain \\begin{equation*}...\\end{equation*} into
    separate text and raw-LaTeX blocks.

    The normalizer's _convert_numbered_equations inserts these equation environments
    into body text. They must NOT be latex-escaped — they're already valid LaTeX.
    """
    # Accept both numbered ("equation") and starred ("equation*") environments
    # so auto-numbered equations produced by the normalizer also pass through
    # as raw_latex (unescaped).
    eq_start = r"\begin{equation}"
    eq_end = r"\end{equation}"
    if eq_start not in para:
        eq_start = r"\begin{equation*}"
        eq_end = r"\end{equation*}"

    if eq_start not in para:
        # No equations — escape normally
        blocks.append({"type": "text", "content": _latex_escape(para)})
        return

    # Split around equation environments
    remaining = para
    while eq_start in remaining:
        idx_start = remaining.index(eq_start)
        idx_end = remaining.find(eq_end, idx_start + len(eq_start))
        if idx_end < 0:
            break

        idx_end += len(eq_end)

        # Text before the equation
        before = remaining[:idx_start].strip()
        if before:
            blocks.append({"type": "text", "content": _latex_escape(before)})

        # The equation itself — pass through as raw LaTeX
        eq_block = remaining[idx_start:idx_end].strip()
        blocks.append({"type": "raw_latex", "content": eq_block})

        remaining = remaining[idx_end:].strip()

    # Any remaining text after last equation
    if remaining.strip():
        blocks.append({"type": "text", "content": _latex_escape(remaining)})


def _fb_to_content_block(fb) -> dict:
    """Convert a FormulaBlock to a content block dict for template rendering."""
    d = _fb_to_dict(fb)
    d["type"] = "equation"
    return d


def _figure_to_dict(f):
    """Convert Figure to dict with LaTeX-safe relative path."""
    raw_path = (
        f.get("image_path", "") if isinstance(f, dict)
        else getattr(f, "image_path", str(f) if isinstance(f, str) else "")
    )
    # Validate image — corrupt PNGs crash pdflatex
    if raw_path:
        abs_path = raw_path if os.path.isabs(raw_path) else os.path.join(INTERMEDIATE_DIR, raw_path)
        if not _is_valid_png(abs_path):
            log.warning("  Skipping corrupt figure image: %s", os.path.basename(raw_path))
            raw_path = ""
    img_path = latex_relpath(raw_path, INTERMEDIATE_DIR)

    caption = f.get("caption", "") if isinstance(f, dict) else getattr(f, "caption", "")
    label = f.get("label", "") if isinstance(f, dict) else getattr(f, "label", "")
    # Strip residual "Fig. N." / "Figure N:" prefix — LaTeX \caption adds its own
    if caption:
        caption = re.sub(
            r"^(?:Fig(?:ure)?\.?\s*\d+[\s\.:]*)",
            "", caption, flags=re.IGNORECASE
        ).strip()
    return {"image_path": img_path, "caption": caption, "label": label}

def _ref_to_dict(r):
    return {"text": r.text, "index": r.index, "author_year": r.author_year}

def _fb_to_dict(fb):
    """Convert FormulaBlock to dict with LaTeX-safe relative image path."""
    raw_img = fb.image_path or ""
    # Validate image before including — corrupt PNGs crash pdflatex
    if raw_img:
        abs_img = raw_img if os.path.isabs(raw_img) else os.path.join(INTERMEDIATE_DIR, raw_img)
        if not _is_valid_png(abs_img):
            log.warning("  Skipping corrupt image: %s", os.path.basename(raw_img))
            raw_img = ""
    img_path = latex_relpath(raw_img, INTERMEDIATE_DIR)

    latex = fb.latex or ""

    # Renderer-side quality gate: when both latex AND image are available,
    # ALWAYS prefer the clean image. pix2tex OCR is non-deterministic and
    # frequently produces structurally valid but semantically wrong LaTeX
    # that renders as garbage. The original equation image is always correct.
    if latex and img_path:
        # Only keep OCR latex for simple, high-confidence formulas that are
        # very likely correct: short, has = sign, no suspicious commands
        if not _is_simple_correct_latex(latex, fb.confidence):
            latex = ""  # force image fallback in the template

    return {"latex": latex, "image_path": img_path,
            "confidence": fb.confidence, "page": fb.page, "label": fb.label,
            "equation_number": fb.equation_number}


def _is_simple_correct_latex(latex: str, confidence: float) -> bool:
    """
    Decide whether OCR LaTeX is trustworthy enough to render instead of the image.

    Strategy: default to IMAGE (always correct). Only use OCR LaTeX when it's
    clearly a simple, correct formula. This is conservative — we'd rather show
    a clean image than risk garbled LaTeX.

    Returns True only for simple, high-confidence formulas.
    """
    if not latex or confidence < OCR_RENDERER_THRESHOLD:
        return False

    # Very short formulas (like E=mc^{2}) are usually correct
    if len(latex) < 30 and "=" in latex:
        return True

    # Medium formulas: require = sign AND no suspicious patterns
    if len(latex) < 80 and "=" in latex:
        # Check for common pix2tex garbage indicators
        suspicious = [
            r"\underbrace", r"\overbrace", r"\stackrel",
            r"\Longleftrightarrow", r"\Longrightarrow", r"\hookrightarrow",
            r"\bigwedge", r"\bigvee", r"\mathcal{C}", r"\mathcal{D}",
            r"\sin\theta",  # misread symbols
            r"\not=", r"\not{",
        ]
        for s in suspicious:
            if s in latex:
                return False
        return True

    # Long formulas (>80 chars) from pix2tex are rarely correct
    return False


# ── Jinja2 custom filters ───────────────────────────────────────

def _latex_escape(text: str) -> str:
    """
    Escape text for LaTeX, but preserve:
    - Already-existing LaTeX commands (\\cmd{...})
    - Inline math ($...$)
    - Display math (\\[...\\], \\(...\\))
    """
    if not text:
        return ""

    # Characters that need escaping in LaTeX
    specials = {
        "&": r"\&",
        "%": r"\%",
        "#": r"\#",
        "_": r"\_",
        "~": r"\textasciitilde{}",
    }

    result = []
    i = 0
    math_depth = 0  # 0 = not in math, >0 = in math

    while i < len(text):
        ch = text[i]

        # Track math mode ($...$ and $$...$$)
        if ch == "$" and (i == 0 or text[i-1] != "\\"):
            # Check for $$ (display math)
            if i + 1 < len(text) and text[i + 1] == "$":
                if math_depth == 0:
                    math_depth = 2  # entering display math
                else:
                    math_depth = 0  # leaving display math
                result.append("$$")
                i += 2
                continue
            # Single $ (inline math)
            if math_depth == 0:
                math_depth = 1
            elif math_depth == 1:
                math_depth = 0
            # Don't toggle if we're in display math ($$) and see single $
            result.append(ch)
            i += 1
            continue

        # Track display math \[...\] and \(...\)
        if ch == "\\" and i + 1 < len(text):
            next_ch = text[i + 1]
            if next_ch == "[":
                math_depth += 1
                result.append("\\[")
                i += 2
                continue
            elif next_ch == "]" and math_depth > 0:
                math_depth -= 1
                result.append("\\]")
                i += 2
                continue
            elif next_ch == "(":
                math_depth += 1
                result.append("\\(")
                i += 2
                continue
            elif next_ch == ")" and math_depth > 0:
                math_depth -= 1
                result.append("\\)")
                i += 2
                continue
            # LaTeX command — pass through
            elif next_ch.isalpha():
                result.append(ch)
                i += 1
                while i < len(text) and text[i].isalpha():
                    result.append(text[i])
                    i += 1
                continue

        # Inside math mode — don't escape
        if math_depth > 0:
            result.append(ch)
            i += 1
            continue

        # Escape special chars
        if ch in specials:
            result.append(specials[ch])
        elif ch == "{":
            result.append(r"\{")
        elif ch == "}":
            result.append(r"\}")
        elif ch == "^":
            result.append(r"\textasciicircum{}")
        else:
            result.append(ch)
        i += 1

    return "".join(result)


def _latex_paragraphs(text: str) -> str:
    """Split text on double newlines into LaTeX paragraphs, with escaping."""
    if not text:
        return ""

    paragraphs = re.split(r"\n\n+", text.strip())
    escaped = []
    for p in paragraphs:
        p = p.strip()
        if p:
            escaped.append(_latex_escape(p))
    return "\n\n".join(escaped)


def _is_numeric_table(headers, rows) -> bool:
    """Check if a table is mostly numeric data (like results tables)."""
    numeric_count = 0
    total_count = 0
    for row in rows:
        for cell in row:
            cell_str = str(cell).strip()
            if cell_str:
                total_count += 1
                # Match numbers with optional decimals, signs, daggers, arrows
                if re.match(r"^[\d\.\-\+±↑↓⇑⇓†‡§\s\*\^]+$", cell_str):
                    numeric_count += 1
    if total_count == 0:
        return False
    return numeric_count / total_count > 0.4


def _render_table(table) -> str:
    """
    Render a Table object as LaTeX tabular environment.

    Uses IEEE-style formatting:
    - Wide tables: use table* for two-column span
    - Numeric tables: center-aligned columns with booktabs-style rules
    - Adaptive column spec based on content
    """
    if isinstance(table, dict):
        headers = table.get("headers", [])
        rows = table.get("rows", [])
        caption = table.get("caption", "")
        label = table.get("label", "")
    else:
        headers = table.headers
        rows = table.rows
        caption = table.caption
        label = table.label

    if not headers and not rows:
        return ""

    num_cols = len(headers) if headers else (len(rows[0]) if rows else 0)
    if num_cols == 0:
        return ""

    # Always use single-column table — adjustbox scales to fit
    is_numeric = _is_numeric_table(headers, rows)

    # Build column spec: first column left-aligned, rest centered for numeric tables
    if is_numeric and num_cols > 2:
        col_spec = "l" + "c" * (num_cols - 1)
    else:
        col_spec = "l" * num_cols

    lines = []

    lines.append(r"\begin{table}[!htbp]")

    # Caption at top for IEEE style
    if caption:
        lines.append(f"\\caption{{{_latex_escape(caption)}}}")
    if label:
        lines.append(f"\\label{{{label}}}")

    lines.append(r"\centering")

    # Use smaller font for wide tables
    if num_cols > 4:
        lines.append(r"\footnotesize")
    if num_cols > 8:
        lines.append(r"\scriptsize")

    lines.append(r"\renewcommand{\arraystretch}{1.2}")

    # adjustbox scales down to fit within single column
    lines.append(r"\adjustbox{max width=\columnwidth}{")

    lines.append(f"\\begin{{tabular}}{{{col_spec}}}")
    lines.append(r"\hline")

    if headers:
        escaped = [f"\\textbf{{{_escape_table_cell(h)}}}" for h in headers]
        lines.append(" & ".join(escaped) + r" \\")
        lines.append(r"\hline")

    for row in rows:
        escaped = [_escape_table_cell(str(c)) for c in row]
        # Pad or trim to match column count
        while len(escaped) < num_cols:
            escaped.append("")
        escaped = escaped[:num_cols]
        lines.append(" & ".join(escaped) + r" \\")

    lines.append(r"\hline")
    lines.append(r"\end{tabular}")
    lines.append("}")  # Close adjustbox

    lines.append(r"\end{table}")

    return "\n".join(lines)


_CELLIMG_RE = re.compile(r"^\\CELLIMG\{(.+?)\}$")
_CELLEQ_RE  = re.compile(r"^\\CELLEQ\{(.+)\}$", re.DOTALL)
_CELLEQ_IMG_SEP = "||IMG:"  # separator between LaTeX and fallback image path

# Garbage indicators in OCR'd table cell LaTeX
_CELLEQ_GARBAGE = [
    r"\\stackrel",       # pix2tex abuses \stackrel for misrecognized fractions
    r"\\bigwedge",       # garbled symbol
    r"\\mathcal\{[CDG]", # nonsensical calligraphic letters
    r"\\not\{",          # slashed symbols from misrecognition
    r"\\mp\\dot",        # gamma-dot artifact
    r"\\langle\\cdot",   # misrecognized angle bracket patterns
]
_CELLEQ_GARBAGE_RE = re.compile("|".join(_CELLEQ_GARBAGE))


def _is_celleq_usable(latex: str) -> bool:
    """
    Quick quality check on CELLEQ LaTeX content.
    Returns False if the LaTeX looks like garbled OCR output.
    """
    if not latex:
        return False

    # Count garbage indicators
    garbage_count = len(_CELLEQ_GARBAGE_RE.findall(latex))

    # More than 2 garbage indicators → definitely garbled
    if garbage_count >= 2:
        return False

    # Very high ratio of backslash commands vs actual content → probably garbled
    commands = re.findall(r"\\[a-zA-Z]+", latex)
    # Simple, correct formulas have few commands relative to their length
    if len(commands) > 8 and len(latex) < 60:
        return False

    # Check for specific known-bad patterns from pix2tex on complex formulas
    # These indicate the OCR completely failed to understand the structure
    if r"\stackrel{" in latex and latex.count(r"\stackrel") >= 2:
        return False

    return True


def _escape_table_cell(text: str) -> str:
    """
    Escape a table cell, preserving math content ($...$) and cell images/equations.

    Table cells may already contain LaTeX math from the normalizer
    (e.g. $a^{2} + b^{2} = c^{2}$). These must NOT be escaped.

    Cells with \\CELLEQ{latex} markers are rendered as display math (selectable text).
    Cells with \\CELLIMG{path} markers fall back to \\includegraphics (image).
    """
    if not text:
        return ""

    stripped = text.strip()

    # Handle pix2tex OCR result: \CELLEQ{latex||IMG:path} → display math or image fallback
    m = _CELLEQ_RE.match(stripped)
    if m:
        content = m.group(1).strip()
        # Extract embedded fallback image path if present
        fallback_img = None
        latex = content
        if _CELLEQ_IMG_SEP in content:
            parts = content.split(_CELLEQ_IMG_SEP, 1)
            latex = parts[0].strip()
            fallback_img = parts[1].strip()

        # Quality gate: reject garbled OCR
        if _is_celleq_usable(latex):
            return f"$\\displaystyle {latex}$"
        elif fallback_img:
            # Fall back to cell image
            fallback_img = latex_relpath(fallback_img, INTERMEDIATE_DIR)
            return (f"\\raisebox{{-0.3\\height}}{{"
                    f"\\includegraphics[max height=0.9cm]{{{fallback_img}}}}}")
        else:
            # No fallback image, render garbled LaTeX as-is (better than nothing)
            return f"$\\displaystyle {latex}$"

    # Handle cell image markers: \CELLIMG{/abs/path/to/image.png}
    # Use \raisebox for vertical centering + constrained height for consistent row sizing
    m = _CELLIMG_RE.match(stripped)
    if m:
        raw_cell_img = m.group(1)
        abs_cell_img = raw_cell_img if os.path.isabs(raw_cell_img) else os.path.join(INTERMEDIATE_DIR, raw_cell_img)
        if not _is_valid_png(abs_cell_img):
            log.warning("  Skipping corrupt table cell image: %s", os.path.basename(raw_cell_img))
            return ""  # empty cell is better than crashing pdflatex
        img_path = latex_relpath(raw_cell_img, INTERMEDIATE_DIR)
        return (f"\\raisebox{{-0.3\\height}}{{"
                f"\\includegraphics[max height=0.9cm]{{{img_path}}}}}")

    # If the entire cell is wrapped in math delimiters, pass through
    if stripped.startswith("$") and stripped.endswith("$"):
        return stripped
    # If cell contains $...$, escape only the non-math parts
    if "$" in text:
        parts = []
        segments = text.split("$")
        for i, seg in enumerate(segments):
            if i % 2 == 0:
                # Outside math — escape
                parts.append(_latex_escape(seg))
            else:
                # Inside math — pass through
                parts.append(f"${seg}$")
        return "".join(parts)
    # No math — normal escape
    return _latex_escape(text)


def _section_cmd(depth) -> str:
    """Map section depth (1-3) to LaTeX section command."""
    depth = int(depth)
    if depth <= 1:
        return r"\section"
    elif depth == 2:
        return r"\subsection"
    else:
        return r"\subsubsection"
