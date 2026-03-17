"""
mapper/base_mapper.py  —  Shared utilities used by all template mappers.

Responsibilities:
  - inject_tables: assign pdfplumber Table dicts to Document sections
  - latex_escape:  escape plain text for LaTeX (math-aware)
  - latex_escape_paragraphs: escape body text preserving RAWTEX blocks
"""

import re
from core.models import Document, Table

# ── LaTeX escape ──────────────────────────────────────────────────────────────

_ESC = {
    "\\": r"\textbackslash{}",
    "&":  r"\&",  "%": r"\%",  "$": r"\$",  "#": r"\#",
    "_":  r"\_",  "{": r"\{",  "}": r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\textasciicircum{}",
}
_MATH_RE = re.compile(
    r"\$\$.*?\$\$|\$[^$\n]+?\$|\\\[.*?\\\]|\\\(.*?\\\)",
    re.DOTALL,
)


def latex_escape(text: str) -> str:
    """Escape LaTeX specials in plain text; leave $math$ untouched."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    result, last = [], 0
    for m in _MATH_RE.finditer(text):
        result.append(_esc_plain(text[last:m.start()]))
        result.append(m.group())
        last = m.end()
    result.append(_esc_plain(text[last:]))
    return "".join(result)


def _esc_plain(text: str) -> str:
    return "".join(_ESC.get(ch, ch) for ch in text)


def latex_escape_paragraphs(text: str) -> str:
    """Escape body text; pass %%RAWTEX%%...%%ENDRAWTEX%% blocks through."""
    if not isinstance(text, str):
        text = str(text)
    parts  = re.split(r"(%%RAWTEX%%.*?%%ENDRAWTEX%%)", text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith("%%RAWTEX%%"):
            result.append(part[len("%%RAWTEX%%"):-len("%%ENDRAWTEX%%")])
        else:
            paras = [latex_escape(re.sub(r"\n"," ",p).strip())
                     for p in part.split("\n\n") if p.strip()]
            result.append("\n\n".join(paras))
    return "\n\n".join(r for r in result if r.strip())


# ── Table injection ───────────────────────────────────────────────────────────

def inject_tables(doc: Document, raw_tables: list):
    """
    Convert raw table dicts (from pdfplumber) into Table objects and
    distribute them across Document sections.

    Assignment priority:
      1. Section body mentions "Table N" matching the caption
      2. Fallback: longest section body (most likely results/experiments)
    """
    if not raw_tables or not doc.sections:
        return

    for rt in raw_tables:
        headers = rt.get("headers") or []
        rows    = [r for r in (rt.get("rows") or []) if any(c.strip() for c in r)]
        if not rows and not headers:
            continue

        table = Table(
            caption=rt.get("caption", "").strip(),
            headers=headers,
            rows=rows,
            notes=rt.get("notes", ""),
        )

        placed = False
        if table.caption:
            m = re.search(r"Table\s+(\d+)", table.caption, re.IGNORECASE)
            if m:
                tag = f"Table {m.group(1)}"
                for sec in doc.sections:
                    if tag in sec.body:
                        sec.tables.append(table)
                        placed = True
                        break

        if not placed:
            best = max(doc.sections, key=lambda s: len(s.body))
            best.tables.append(table)