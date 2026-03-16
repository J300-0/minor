"""
stages/template_renderer.py
Stage 4 — Template Renderer: Document → LaTeX document

Responsibility:
  - Load the Jinja2 template
  - Escape plain text for LaTeX while PRESERVING math expressions
  - Write the rendered .tex file

Key rule: content inside $...$ or $$...$$ is already valid LaTeX math
and must NOT be escaped. Only plain text portions get escaped.
"""

import os
import re
import shutil
from jinja2 import Environment, FileSystemLoader
from core.models import Document


# ── LaTeX special character escape map (plain text only) ─────────────────────
_ESCAPE_MAP: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "{":  r"\{",
    "}":  r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\textasciicircum{}",
}

# Matches inline math $...$ and display math $$...$$
# Also matches \( ... \) and \[ ... \] forms
_MATH_RE = re.compile(
    r"""
    \$\$.*?\$\$          # display math $$...$$
    | \$[^$\n]+?\$        # inline math $...$
    | \\\[.*?\\\]         # display math \[...\]
    | \\\(.*?\\\)         # inline math \(...\)
    """,
    re.VERBOSE | re.DOTALL,
)


def _escape_plain(text: str) -> str:
    """Escape LaTeX special chars in a plain (non-math) string."""
    result = []
    for ch in text:
        result.append(_ESCAPE_MAP.get(ch, ch))
    return "".join(result)


def latex_escape(text: str) -> str:
    """
    Escape LaTeX special chars in text, but leave math expressions untouched.
    Splits on math delimiters, escapes only the plain-text segments.
    """
    if not text:
        return ""

    result = []
    last = 0
    for m in _MATH_RE.finditer(text):
        # Escape the plain text before this math span
        result.append(_escape_plain(text[last:m.start()]))
        # Pass the math expression through unchanged
        result.append(m.group())
        last = m.end()
    # Escape any remaining plain text after the last math span
    result.append(_escape_plain(text[last:]))
    return "".join(result)


def latex_escape_paragraphs(text: str) -> str:
    """
    Escape text preserving paragraph breaks and math.
    Paragraphs wrapped in %%RAWTEX%%...%%ENDRAWTEX%% are passed through
    completely unescaped (used for pre-built LaTeX like table environments).
    """
    # Split on RAWTEX sentinels
    parts = re.split(r"(%%RAWTEX%%.*?%%ENDRAWTEX%%)", text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith("%%RAWTEX%%"):
            # Strip sentinels and pass raw LaTeX through unchanged
            raw = part[len("%%RAWTEX%%"):-len("%%ENDRAWTEX%%")]
            result.append(raw)
        else:
            # Normal text: escape each paragraph, collapse single newlines
            paragraphs = part.split("\n\n")
            escaped = []
            for p in paragraphs:
                p = re.sub(r"\n", " ", p).strip()
                if p:
                    escaped.append(latex_escape(p))
            result.append("\n\n".join(escaped))
    return "\n\n".join(r for r in result if r.strip())


# ── Renderer ──────────────────────────────────────────────────────────────────

def render(doc: Document, template_dir: str, template_name: str, output_tex: str):
    # IEEEtran.cls must sit next to the .tex when pdflatex runs
    cls_src = os.path.join(template_dir, "IEEEtran.cls")
    cls_dst = os.path.join(os.path.dirname(output_tex), "IEEEtran.cls")
    if os.path.exists(cls_src) and not os.path.exists(cls_dst):
        shutil.copy2(cls_src, cls_dst)

    env = Environment(
        loader=FileSystemLoader(template_dir),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        line_statement_prefix="%%",
        line_comment_prefix="%#",
        trim_blocks=True,
        autoescape=False,
    )

    env.filters["e"]      = latex_escape
    env.filters["escape"] = latex_escape
    env.filters["paras"]  = latex_escape_paragraphs

    template = env.get_template(template_name)
    rendered = template.render(**doc.to_dict())

    os.makedirs(os.path.dirname(output_tex), exist_ok=True)
    with open(output_tex, "w", encoding="utf-8") as f:
        f.write(rendered)

    print(f"         → {output_tex}")
    return output_tex