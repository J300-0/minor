"""
renderer/jinja_renderer.py -- Stage 4: Document -> .tex via Jinja2.

LaTeX-safe Jinja2 delimiters:
  Variables : \\VAR{ name }
  Blocks    : \\BLOCK{ for x in xs } ... \\BLOCK{ endfor }
  Comments  : \\#{ comment }
  Line stmt : %% (prefix)
"""
import os, re, shutil, traceback
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from core.models import Document, Section, Author, Reference
from core.logger import get_logger

log = get_logger(__name__)

TEMPLATE_FILE = "template.tex.j2"


def render(doc: Document, template_name: str, template_dir: str, output_tex: str) -> str:
    doc = _sanitize(doc)
    out_dir = os.path.dirname(output_tex)

    # Copy required .cls file next to the output .tex
    from core.config import CLS_FILES
    cls = CLS_FILES.get(template_name)
    if cls:
        src = os.path.join(template_dir, cls)
        dst = os.path.join(out_dir, cls)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    # Copy figure images next to the .tex so \includegraphics can find them
    for section in doc.sections:
        for fig in section.figures:
            if fig.image_path and os.path.exists(fig.image_path):
                fname = os.path.basename(fig.image_path)
                dst = os.path.join(out_dir, fname)
                if not os.path.exists(dst):
                    shutil.copy2(fig.image_path, dst)
                fig.image_path = fname  # use relative path in .tex

    env = Environment(
        loader               = FileSystemLoader(template_dir),
        block_start_string   = r"\BLOCK{",
        block_end_string     = "}",
        variable_start_string= r"\VAR{",
        variable_end_string  = "}",
        comment_start_string = r"\#{",
        comment_end_string   = "}",
        line_statement_prefix= "%%",
        trim_blocks          = True,
        lstrip_blocks        = True,
        autoescape           = False,
        undefined            = StrictUndefined,
    )
    env.filters["e"]           = latex_escape
    env.filters["paras"]       = latex_paragraphs
    env.filters["render_table"] = render_table

    try:
        tex = env.get_template(TEMPLATE_FILE).render(
            title      = doc.title,
            authors    = doc.authors,
            abstract   = doc.abstract,
            keywords   = doc.keywords,
            sections   = doc.sections,
            references = doc.references,
        )
    except Exception as exc:
        log.error("Jinja2 render error:\n%s", traceback.format_exc())
        print(f"\n  ❌  Renderer error: {exc}")
        print(f"      Check logs/pipeline_latest.log for details")
        raise

    os.makedirs(os.path.dirname(output_tex), exist_ok=True)
    with open(output_tex, "w", encoding="utf-8") as f:
        f.write(tex)
    print(f"         → {output_tex}")
    return output_tex


# ── LaTeX escape filters ──────────────────────────────────────────────────────

_SPECIAL = [
    ("\\", r"\textbackslash{}"),
    ("&",  r"\&"),
    ("%",  r"\%"),
    ("$",  r"\$"),
    ("#",  r"\#"),
    ("_",  r"\_"),
    ("{",  r"\{"),
    ("}",  r"\}"),
    ("~",  r"\textasciitilde{}"),
    ("^",  r"\textasciicircum{}"),
]


def latex_escape(text) -> str:
    if not text:
        return ""
    text = str(text)
    # Preserve already-escaped LaTeX math ($...$)
    parts = re.split(r"(\$[^$]*\$)", text)
    out = []
    for part in parts:
        if part.startswith("$") and part.endswith("$"):
            out.append(part)   # math — don't escape
        else:
            for ch, rep in _SPECIAL:
                part = part.replace(ch, rep)
            out.append(part)
    return "".join(out)


def latex_paragraphs(text) -> str:
    """Escape text and separate paragraphs with \\n\\n (LaTeX paragraph break)."""
    if not text:
        return ""
    paras = re.split(r"\n\n+", str(text))
    return "\n\n".join(latex_escape(p.strip()) for p in paras if p.strip())


# ── Table renderer ───────────────────────────────────────────────────────────

def render_table(table) -> str:
    """Render a Table object to a LaTeX tabular environment string.

    For wide tables (>5 columns), wraps the tabular in \\resizebox
    to fit within column width and prevent overflow.
    """
    headers = table.headers or []
    rows    = table.rows    or []
    ncols   = max(len(headers), max((len(r) for r in rows), default=0), 1)
    col_spec = "|" + "l|" * ncols
    wide = ncols > 5  # needs resizing to fit single-column layouts

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
    ]
    if wide:
        lines.append(r"\small")  # smaller font for wide tables
    if table.caption:
        lines.append(r"\caption{" + latex_escape(table.caption) + "}")
    if wide:
        lines.append(r"\resizebox{\columnwidth}{!}{%")
    lines += [
        r"\begin{tabular}{" + col_spec + "}",
        r"\hline",
    ]
    if headers:
        row_tex = " & ".join(r"\textbf{" + latex_escape(h) + "}" for h in headers)
        lines += [row_tex + r" \\", r"\hline"]
    for row in rows:
        # Pad row to ncols if needed
        cells = list(row) + [""] * (ncols - len(row))
        lines.append(" & ".join(latex_escape(c) for c in cells) + r" \\")
    lines += [r"\hline", r"\end{tabular}"]
    if wide:
        lines.append("}")  # close \resizebox
    lines.append(r"\end{table}")
    return "\n".join(lines)


# ── Document sanitizer ────────────────────────────────────────────────────────

def _sanitize(doc: Document) -> Document:
    """Ensure all string fields are non-None strings."""
    from copy import deepcopy
    doc = deepcopy(doc)
    doc.title    = str(doc.title or "Untitled")
    doc.abstract = str(doc.abstract or "")
    doc.keywords = [str(k) for k in (doc.keywords or []) if k]
    for a in doc.authors:
        a.name         = str(a.name or "")
        a.department   = str(a.department or "")
        a.organization = str(a.organization or "")
        a.city         = str(a.city or "")
        a.country      = str(a.country or "")
        a.email        = str(a.email or "")
    for s in doc.sections:
        s.heading = str(s.heading or "")
        s.body    = str(s.body or "")
    for r in doc.references:
        r.text = str(r.text or "")
    return doc
