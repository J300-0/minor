"""
stages/template_renderer.py
Stage 4 — Document → LaTeX via Jinja2 template

Key rules:
  - Content inside $...$ or $$...$$ is preserved unchanged (math)
  - %%RAWTEX%%...%%ENDRAWTEX%% blocks pass through unescaped (pre-built LaTeX)
  - Section.tables are rendered as booktabs LaTeX tables directly in template
  - References use ref.index and ref.text (Reference dataclass)
  - Dataclass objects are passed to Jinja2 as-is (attribute access works)
"""

import os
import re
import shutil
from jinja2 import Environment, FileSystemLoader
from core.models import Document


_ESCAPE_MAP = {
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
    result = []
    for ch in text:
        result.append(_ESCAPE_MAP.get(ch, ch))
    return "".join(result)


def latex_escape(text: str) -> str:
    """Escape LaTeX special chars but leave math expressions untouched."""
    if not text:
        return ""
    # Handle non-string inputs gracefully (e.g. if a dataclass leaks in)
    if not isinstance(text, str):
        text = str(text)

    result = []
    last   = 0
    for m in _MATH_RE.finditer(text):
        result.append(_escape_plain(text[last:m.start()]))
        result.append(m.group())
        last = m.end()
    result.append(_escape_plain(text[last:]))
    return "".join(result)


def latex_escape_paragraphs(text: str) -> str:
    """
    Escape text preserving paragraph breaks and math.
    %%RAWTEX%%...%%ENDRAWTEX%% blocks are passed through unescaped.
    """
    if not isinstance(text, str):
        text = str(text)

    parts  = re.split(r"(%%RAWTEX%%.*?%%ENDRAWTEX%%)", text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith("%%RAWTEX%%"):
            raw = part[len("%%RAWTEX%%"):-len("%%ENDRAWTEX%%")]
            result.append(raw)
        else:
            paragraphs = part.split("\n\n")
            escaped    = []
            for p in paragraphs:
                p = re.sub(r"\n", " ", p).strip()
                if p:
                    escaped.append(latex_escape(p))
            result.append("\n\n".join(escaped))
    return "\n\n".join(r for r in result if r.strip())


def render(doc: Document, template_dir: str, template_name: str, output_tex: str):
    # IEEEtran.cls must sit next to the .tex when pdflatex runs
    cls_src = os.path.join(template_dir, "IEEEtran.cls")
    cls_dst = os.path.join(os.path.dirname(output_tex), "IEEEtran.cls")
    if os.path.exists(cls_src) and not os.path.exists(cls_dst):
        shutil.copy2(cls_src, cls_dst)

    env = Environment(
        loader               = FileSystemLoader(template_dir),
        block_start_string   = r"\BLOCK{",
        block_end_string     = "}",
        variable_start_string= r"\VAR{",
        variable_end_string  = "}",
        comment_start_string = r"\#{",
        comment_end_string   = "}",
        line_statement_prefix= "%%",
        line_comment_prefix  = "%#",
        trim_blocks          = True,
        autoescape           = False,
    )

    env.filters["e"]      = latex_escape
    env.filters["escape"] = latex_escape
    env.filters["paras"]  = latex_escape_paragraphs

    template = env.get_template(template_name)

    # Pass dataclass fields as attribute-accessible objects
    # Jinja2 handles dataclasses natively via attribute access
    rendered = template.render(**doc.to_dict_with_objects())

    os.makedirs(os.path.dirname(output_tex), exist_ok=True)
    with open(output_tex, "w", encoding="utf-8") as f:
        f.write(rendered)

    print(f"         → {output_tex}")
    return output_tex