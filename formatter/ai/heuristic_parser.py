"""
ai/heuristic_parser.py  —  Stage 2B: heuristic document structure detection

Used when LM Studio is unavailable. Also exports:
  - extract_references(raw) → list[Reference]  (used by AI path as fallback)
  - inject_tables(doc, raw_tables)              (shared by both paths)
"""

import re
from core.logger import get_logger
log = get_logger(__name__)
from core.models import Document, Section, Author, Table, Reference

_SECTION_NAMES = {
    "abstract","introduction","related work","background","methodology","methods",
    "approach","system design","implementation","experiments","experimental study",
    "evaluation","results","discussion","conclusion","conclusions","future work",
    "acknowledgment","acknowledgements","references","bibliography",
}
_LEAD = re.compile(r"^(\d+(\.\d+)*|[IVXLCDM]{2,})\.?\s+")


def parse(extracted_path: str, rich: dict) -> Document:
    """
    Main entry point. rich must be a dict {raw_text, tables, images}.
    Never raises — returns a best-effort Document.
    """
    # ── Guard: rich must be a dict ─────────────────────────────────────────
    if not isinstance(rich, dict):
        rich = {"raw_text": "", "tables": [], "images": []}

    with open(extracted_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    blocks      = _build_blocks(raw)
    doc         = Document()
    doc.title   = _find_title(blocks)
    doc.authors = _find_authors(blocks, doc.title)
    _parse_body(blocks, doc)

    # Standalone ref extractor is more reliable than block-by-block accumulation
    standalone = extract_references(raw)
    if len(standalone) > len(doc.references):
        doc.references = standalone

    # Inject pdfplumber tables
    if isinstance(rich, dict) and rich.get("tables"):
        from mapper.base_mapper import inject_tables
        inject_tables(doc, rich["tables"])

    return doc


# ── Reference extractor (standalone, exported) ────────────────────────────────

def extract_references(raw: str) -> list:
    """
    Extract references from raw text.
    Handles three formats:
      [N] Author. Title. (IEEE/ACM style)
      N.  Author. Title. (Springer/Elsevier style — multi-line, blank-line separated)
      N.  Author. Title. (same but continuation lines indented)
    """
    # Find references section start
    ref_start = None
    for pattern in [r"\nReferences\s*\n", r"\nBibliography\s*\n", r"\nREFERENCES\s*\n"]:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            ref_start = m.end()
            break
    if ref_start is None:
        m = re.search(r"(?:^|\n)\[1\]", raw)
        ref_start = m.start() if m else None
    if ref_start is None:
        return []

    ref_text = raw[ref_start:].replace("\r\n", "\n").replace("\r", "\n")

    # Springer uses blank lines between entries — split on those first
    # then process each entry as a single unit
    entries = re.split(r"\n{2,}", ref_text.strip())
    refs    = []

    # Pattern: starts with [N] or N. 
    _REF_START = re.compile(r"^\[(\d+)\]\s*(.*)$|^(\d+)\.\s+(.+)$", re.DOTALL)

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        # Multi-line entry — join continuation lines
        lines = [l.strip() for l in entry.split("\n") if l.strip()]
        if not lines:
            continue

        m = _REF_START.match(lines[0])
        if m:
            if m.group(1):  # [N] format
                idx  = int(m.group(1))
                text = m.group(2).strip()
            else:           # N. format
                idx  = int(m.group(3))
                text = m.group(4).strip()

            # Append continuation lines
            for cont in lines[1:]:
                text += " " + cont
            text = re.sub(r"\s{2,}", " ", text).strip()
            if text:
                refs.append(Reference(index=idx, text=text))
        elif refs:
            # Continuation of previous entry (no number prefix)
            for line in lines:
                refs[-1].text += " " + line
            refs[-1].text = re.sub(r"\s{2,}", " ", refs[-1].text).strip()

    # Sort and deduplicate
    seen, out = set(), []
    for r in sorted(refs, key=lambda r: r.index):
        key = r.text[:60]
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── Block builder ─────────────────────────────────────────────────────────────

def _build_blocks(raw: str) -> list:
    chunks = [c.strip() for c in raw.split("\n\n") if c.strip()]
    chunks = _merge_orphan_numbers(chunks)
    blocks = []
    for chunk in chunks:
        if not blocks:
            blocks.append(chunk); continue
        prev = blocks[-1]
        if any(x.startswith("__IMAGE__") or x.startswith("$$") for x in (prev, chunk)):
            blocks.append(chunk); continue
        if _detect_heading(prev) or _detect_heading(chunk):
            blocks.append(chunk); continue
        if prev.lower() in ("abstract","references","bibliography") or \
           chunk.lower() in ("abstract","references","bibliography"):
            blocks.append(chunk); continue
        if _is_meta(chunk) or _is_meta(prev):
            blocks.append(chunk); continue
        if _is_continuation(prev, chunk):
            blocks[-1] = prev + " " + chunk
        else:
            blocks.append(chunk)
    return blocks


def _merge_orphan_numbers(chunks):
    out, i = [], 0
    while i < len(chunks):
        c = chunks[i]
        if re.match(r"^\d+(\.\d+)?\.?$", c) and i + 1 < len(chunks):
            out.append(c + "\n" + chunks[i+1]); i += 2
        else:
            out.append(c); i += 1
    return out


def _is_meta(line):
    line = line.strip()
    if "\n" in line: return False
    if "@" in line: return True
    if len(line) < 60 and not re.search(r"[.!?]$", line) and re.match(r"^[A-Z]", line):
        words = line.split()
        if len(words) <= 6 and all(w[0].isupper() for w in words if w):
            return True
    return False


def _is_continuation(prev, curr):
    last = prev.rstrip()[-1] if prev.rstrip() else ""
    if last in (".", "!", "?", '"', "'"):
        lw = prev.rstrip().rsplit(None, 1)[-1]
        if not (len(lw) <= 5 and lw.endswith(".") or lw in ("e.g.","i.e.","etc.","vs.","cf.")):
            return False
    if re.match(r"^[•\-\*]|^\d+\.|^[A-Z]{2,}\s", curr):
        return False
    return True


# ── Title / authors ───────────────────────────────────────────────────────────

# Lines that look like journal/publisher headers, not paper titles
_HEADER_NOISE = re.compile(
    r"""^(
        https?://                           # URL / DOI link
      | doi\.org/                           # DOI
      | \d{4}-\d{4}                         # ISSN
      | received:                           # submission date
      | accepted:
      | published:
      | \©
      | the\s+author
      | open\s+access
      | research\s+article
      | original\s+article
      | letter
      | [A-Za-z]+\s+learning\s+\(\d{4}\)   # "Machine Learning (2026)"
      | [A-Za-z]+\s+\(\d{4}\)\s+\d          # "Journal (2026) 115"
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def _find_title(blocks):
    """
    Return the paper title — the first block that does NOT look like a
    journal header, DOI, date line, or publisher boilerplate.
    Springer PDFs often start with 'Machine Learning (2026) 115:72'
    before the actual title.
    """
    for b in blocks:
        first = b.strip().split("\n")[0].strip()
        if not first:
            continue
        # Skip noise lines
        if _HEADER_NOISE.match(first):
            continue
        # Skip very short lines that are clearly not titles (page numbers, etc.)
        if len(first) < 8:
            continue
        # Skip lines that are purely uppercase acronyms / section labels
        if re.match(r"^[A-Z\s]{2,20}$", first) and len(first.split()) <= 3:
            continue
        return first
    return "Untitled"


def _find_authors(blocks, title):
    authors = []
    for block in blocks[1:15]:
        if block.lower() == "abstract" or _detect_heading(block): break
        if block == title: continue
        # Springer "·" separated
        if "·" in block or "\u00b7" in block:
            for part in re.split(r"\s*[·\u00b7]\s*", block.replace("\n"," ")):
                name = re.sub(r"[\d,\s]+$", "", part).strip()
                if re.match(r"^[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+(\s+[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+)+$", name) \
                   and len(name) < 60 and not re.search(r"\d", name):
                    authors.append(Author(name=name))
            if authors: return authors
            continue
        # DOCX multi-line
        if "\n" in block:
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if lines and re.match(r"^[\w\u00C0-\u024F][\w\u00C0-\u024F\-]*(\s+[\w\u00C0-\u024F][\w\u00C0-\u024F\-]*)+$", lines[0]) \
               and len(lines[0]) < 60 and not re.search(r"\d", lines[0]):
                a = Author(name=lines[0])
                for l in lines[1:]:
                    lo = l.lower()
                    if "@" in l: a.email = l
                    elif re.match(r"^(dept|department|school|faculty)", lo): a.department = l
                    elif re.search(r"(university|institute|college|lab\b)", lo): a.organization = l
                    elif not a.organization: a.organization = l
                authors.append(a)
            continue
    return authors


# ── Body parser ───────────────────────────────────────────────────────────────

def _parse_body(blocks, doc):
    cur_head, cur_body = None, []
    next_abstract, in_refs = False, False
    author_names = {a.name for a in doc.authors}

    def flush():
        if cur_head and cur_body:
            doc.sections.append(Section(heading=cur_head, body="\n\n".join(cur_body).strip()))

    for block in blocks:
        low, first = block.lower().strip(), block.split("\n")[0].strip()
        if first == doc.title or first in author_names: continue
        if block.startswith("__IMAGE__"): continue

        if low == "abstract": next_abstract = True; continue
        if next_abstract: doc.abstract = block.strip(); next_abstract = False; continue
        if re.match(r"^abstract[\s:—\-]+\S", block, re.IGNORECASE) and not doc.abstract:
            doc.abstract = re.sub(r"^abstract[\s:—\-]+","",block,flags=re.IGNORECASE).strip(); continue
        if re.match(r"^(keywords?|index terms?)[\s:—\-]", low):
            kw = re.sub(r"^(keywords?|index terms?)[\s:—\-]+","",block,flags=re.IGNORECASE)
            doc.keywords = [k.strip() for k in re.split(r"[;,]", kw) if k.strip()]; continue
        if re.match(r"^references\.?$", low):
            flush(); cur_head = cur_body = None; cur_body = []; in_refs = True; continue
        if in_refs: continue   # handled by extract_references()

        if block.startswith("$$") and block.endswith("$$"):
            if cur_head:
                inner = block[2:-2].strip()
                cur_body.append("%%RAWTEX%%\\begin{equation}\n"+inner+"\n\\end{equation}%%ENDRAWTEX%%")
            continue

        heading = _detect_heading(block)
        if heading:
            flush(); cur_head = heading; cur_body = []; continue

        if cur_head:
            cur_body.append(block.strip())

    flush()


# ── Heading detection ─────────────────────────────────────────────────────────

def _detect_heading(block):
    s = block.strip()
    lines = s.split("\n")
    if len(lines) == 2:
        num, text = lines[0].strip(), lines[1].strip()
        if re.match(r"^\d+(\.\d+)*\.?$|^[ivxlcdmIVXLCDM]+\.?$", num, re.IGNORECASE):
            clean = _LEAD.sub("", text).strip().rstrip(".")
            if clean.lower() in _SECTION_NAMES or len(clean) < 60 and not re.search(r"[.!?]", clean):
                return clean
    if len(s) <= 80:
        m = re.match(r"^([IVXLCDM]+)\.\s+(.+)$", s)
        if m and len(s) < 60:
            return m.group(2).title() if m.group(2).isupper() else m.group(2)
        m = re.match(r"^([A-Z])\.\s+(.+)$", s)
        if m and len(s) < 80 and not re.search(r"[.!?]$", s):
            return m.group(2)
        clean = _LEAD.sub("", s).strip().rstrip(".")
        if clean.lower() in _SECTION_NAMES: return clean
        if s.isupper() and 3 < len(s) < 60: return s.title()
    return None