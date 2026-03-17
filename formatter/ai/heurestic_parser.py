"""
ai/heuristic_parser.py  —  Stage 2B: heuristic document structure detection

Used when LM Studio is unavailable. Also exports:
  - extract_references(raw) → list[Reference]  (used by AI path as fallback)
  - inject_tables(doc, raw_tables)              (shared by both paths)
"""

import re
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
    Extract references directly from raw text.
    Works on [N] format and bare N. format (Springer).
    Handles multi-line entries split across text blocks.
    Returns list[Reference].
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
    lines    = ref_text.split("\n")
    refs, current_idx, current_text = [], None, []

    def _flush():
        if current_idx is not None and current_text:
            text = re.sub(r"\s{2,}", " ", " ".join(current_text).strip())
            refs.append(Reference(index=current_idx, text=text))

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(\d+)\]\s*(.*)", line) or re.match(r"^(\d+)\.\s+(.*)", line)
        if m:
            _flush()
            current_idx  = int(m.group(1))
            current_text = [m.group(2).strip()] if m.group(2).strip() else []
        elif current_idx is not None:
            current_text.append(line)

    _flush()
    return refs


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

def _find_title(blocks):
    for b in blocks:
        if b.strip():
            return b.strip().split("\n")[0].strip()
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