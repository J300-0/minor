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

log = logging.getLogger("paper_formatter")


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
    return {
        "heading": s.heading, "depth": s.depth, "body": s.body,
        "tables": s.tables,
        "figures": [_figure_to_dict(f) for f in s.figures],
        "formula_blocks": [_fb_to_dict(fb) for fb in s.formula_blocks],
    }


def _figure_to_dict(f):
    """Convert Figure to dict with LaTeX-safe relative path."""
    img_path = f.image_path if isinstance(f, str) else (
        f.get("image_path", "") if isinstance(f, dict) else f.image_path
    )
    # Convert absolute path to relative from intermediate dir
    # pdflatex runs in intermediate/, figures are in intermediate/figures/
    if img_path and os.path.isabs(img_path):
        try:
            img_path = os.path.relpath(img_path, INTERMEDIATE_DIR)
        except ValueError:
            pass  # different drive on Windows, keep absolute
    # Normalize path separators for LaTeX (always use forward slashes)
    img_path = img_path.replace("\\", "/")

    caption = f.get("caption", "") if isinstance(f, dict) else f.caption
    label = f.get("label", "") if isinstance(f, dict) else f.label
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
    img_path = fb.image_path or ""
    if img_path and os.path.isabs(img_path):
        try:
            img_path = os.path.relpath(img_path, INTERMEDIATE_DIR)
        except ValueError:
            pass
    img_path = img_path.replace("\\", "/")

    return {"latex": fb.latex, "image_path": img_path,
            "confidence": fb.confidence, "page": fb.page, "label": fb.label}


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

        # Track inline math ($...$)
        if ch == "$" and (i == 0 or text[i-1] != "\\"):
            if math_depth == 0:
                math_depth = 1
            else:
                math_depth = 0
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

    # Handle pix2tex OCR result: \CELLEQ{latex} → \[ latex \] (display math, selectable)
    m = _CELLEQ_RE.match(stripped)
    if m:
        latex = m.group(1).strip()
        return f"$\\displaystyle {latex}$"

    # Handle cell image markers: \CELLIMG{/abs/path/to/image.png}
    # Use \raisebox for vertical centering + constrained height for consistent row sizing
    m = _CELLIMG_RE.match(stripped)
    if m:
        img_path = m.group(1)
        # Convert absolute path to relative from intermediate dir
        if os.path.isabs(img_path):
            try:
                img_path = os.path.relpath(img_path, INTERMEDIATE_DIR)
            except ValueError:
                pass
        img_path = img_path.replace("\\", "/")
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
