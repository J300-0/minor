"""
templates/renderer.py  —  Single shared Jinja2 renderer for all templates.

Called by pipeline._render() directly. No per-template mapper.py needed.
Each template only needs a template.tex.j2 file.
"""

import os, shutil
from jinja2 import Environment, FileSystemLoader
from core.models import Document
from mapper.base_mapper import latex_escape, latex_escape_paragraphs


TEMPLATE_FILE = "template.tex.j2"

# .cls files that need to be copied next to the .tex before pdflatex runs
_CLS_FILES = {
    "ieee":     "IEEEtran.cls",
    "springer": "IEEEtran.cls",
}


def render(doc: Document, template_name: str, template_dir: str, output_tex: str) -> str:
    # Copy any required .cls file next to the output .tex
    cls = _CLS_FILES.get(template_name)
    if cls:
        src = os.path.join(template_dir, cls)
        dst = os.path.join(os.path.dirname(output_tex), cls)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    env = _make_env(template_dir)
    tex = env.get_template(TEMPLATE_FILE).render(**doc.to_dict_with_objects())

    os.makedirs(os.path.dirname(output_tex), exist_ok=True)
    with open(output_tex, "w", encoding="utf-8") as f:
        f.write(tex)
    print(f"         → {output_tex}")
    return output_tex


def _make_env(template_dir: str) -> Environment:
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
        autoescape           = False,
    )
    env.filters["e"]     = latex_escape
    env.filters["paras"] = latex_escape_paragraphs
    return env