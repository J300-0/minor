"""
stages/document_parser.py
Stage 2B (heuristic fallback) — raw text + rich layout dict → Document

Changes vs old version:
  - Accepts `rich` dict from layout_parser (has pre-extracted tables[])
  - Tables from pdfplumber are injected into sections directly
    instead of being re-detected from flat text (which was broken)
  - References now stored as Reference(index, text) objects not plain strings
  - _parse_body no longer tries to re-detect tables from flat text
    (pdfplumber already got them; we just need to assign them to sections)
"""

import re
from core.models import Document, Section, Author, Table, Reference


# ── Known IEEE/academic section names ────────────────────────────────────────
IEEE_SECTION_NAMES = {
    "abstract", "introduction", "related work", "background",
    "methodology", "methods", "approach", "system design",
    "implementation", "experiments", "experimental study",
    "evaluation", "results", "discussion",
    "conclusion", "conclusions", "future work",
    "acknowledgment", "acknowledgements", "references", "bibliography",
}

_LEADING_NUM = re.compile(r"^(\d+(\.\d+)*|[IVXLCDM]{2,})\.?\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse(extracted_path: str, rich: dict = None) -> Document:
    """
    Parse extracted text into a Document.
    rich: the dict returned by layout_parser.parse() containing pre-extracted tables.
    """
    with open(extracted_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    pre_tables = rich.get("tables", []) if rich else []

    blocks = _build_blocks(raw)

    doc         = Document()
    doc.title   = _find_title(blocks)
    doc.authors = _find_authors(blocks, doc.title)
    _parse_body(blocks, doc)

    # Always run the standalone ref extractor and take the better result.
    # Block-by-block accumulation often misses wrapped/fragmented entries.
    standalone_refs = _extract_references_from_text(raw)
    if len(standalone_refs) > len(doc.references):
        doc.references = standalone_refs

    # Inject pre-extracted tables from pdfplumber into sections
    if pre_tables:
        _inject_tables(doc, pre_tables)

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Inject pdfplumber tables into document sections
# ─────────────────────────────────────────────────────────────────────────────

def _inject_tables(doc: Document, raw_tables: list):
    """
    Convert raw table dicts from layout_parser into Table objects and
    distribute them across sections. Tables are assigned to the section
    whose caption text most closely matches the table's caption, or
    appended to the longest section if no match is found.
    """
    if not doc.sections:
        return

    for rt in raw_tables:
        headers = rt.get("headers") or []
        rows    = rt.get("rows") or []
        caption = rt.get("caption", "").strip()
        notes   = rt.get("notes", "")

        # Skip empty tables
        if not rows and not headers:
            continue

        # Clean up: filter completely empty rows
        rows = [r for r in rows if any(c.strip() for c in r)]

        table = Table(
            caption=caption,
            headers=headers,
            rows=rows,
            notes=notes,
        )

        # Try to place table in the section whose body mentions this caption
        placed = False
        if caption:
            for sec in doc.sections:
                # Match on "Table N" prefix
                m = re.search(r"Table\s+(\d+)", caption, re.IGNORECASE)
                if m and f"Table {m.group(1)}" in sec.body:
                    sec.tables.append(table)
                    placed = True
                    break

        if not placed:
            # Append to the longest body section (most likely experimental/results)
            best = max(doc.sections, key=lambda s: len(s.body))
            best.tables.append(table)


# ─────────────────────────────────────────────────────────────────────────────
# Block builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_blocks(raw: str) -> list:
    chunks = [c.strip() for c in raw.split("\n\n") if c.strip()]
    chunks = _merge_orphan_numbers(chunks)

    blocks = []
    for chunk in chunks:
        if not blocks:
            blocks.append(chunk)
            continue

        prev = blocks[-1]

        if _detect_heading(prev) or _detect_heading(chunk):
            blocks.append(chunk)
            continue
        if prev.lower() in ("abstract", "references", "bibliography"):
            blocks.append(chunk)
            continue
        if chunk.lower() in ("abstract", "references", "bibliography"):
            blocks.append(chunk)
            continue
        if chunk.startswith("__IMAGE__") or prev.startswith("__IMAGE__"):
            blocks.append(chunk)
            continue
        if chunk.startswith("$$") or prev.startswith("$$"):
            blocks.append(chunk)
            continue
        if _is_metadata_line(chunk) or _is_metadata_line(prev):
            blocks.append(chunk)
            continue
        if _is_continuation(prev, chunk):
            blocks[-1] = prev + " " + chunk
        else:
            blocks.append(chunk)

    return blocks


def _merge_orphan_numbers(chunks: list) -> list:
    merged = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        if re.match(r"^\d+(\.\d+)?\.?$", c) and i + 1 < len(chunks):
            merged.append(c + "\n" + chunks[i + 1])
            i += 2
        else:
            merged.append(c)
            i += 1
    return merged


def _is_metadata_line(line: str) -> bool:
    line = line.strip()
    if "\n" in line:
        return False
    if "@" in line:
        return True
    if re.match(r"^\(?\d[\d\s\-().]{5,}$", line):
        return True
    if re.match(r"^[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{0,5}$", line):
        return True
    if len(line) < 60 and not re.search(r"[.!?]$", line) and re.match(r"^[A-Z]", line):
        words = line.split()
        if len(words) <= 6 and all(w[0].isupper() for w in words if w):
            return True
    return False


def _is_continuation(prev: str, curr: str) -> bool:
    prev_last = prev.rstrip()[-1] if prev.rstrip() else ""
    if prev_last in (".", "!", "?", '"', "'"):
        last_word = prev.rstrip().rsplit(None, 1)[-1]
        is_abbrev = (
            len(last_word) <= 5 and last_word.endswith(".")
            or last_word in ("e.g.", "i.e.", "etc.", "vs.", "cf.")
        )
        if not is_abbrev:
            return False
    if re.match(r"^[•\-\*]|^\d+\.|^[A-Z]{2,}\s", curr):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Field extractors
# ─────────────────────────────────────────────────────────────────────────────

def _find_title(blocks: list) -> str:
    for b in blocks:
        if b.strip():
            return b.strip().split("\n")[0].strip()
    return "Untitled"


def _find_authors(blocks: list, title: str) -> list:
    authors = []

    for block in blocks[1:15]:
        if block.lower() == "abstract" or _detect_heading(block):
            break
        if block == title:
            continue

        # Springer format: "Name1 · Name2 · Name3"
        if "·" in block or "\u00b7" in block:
            raw = block.replace("\n", " ")
            parts = re.split(r"\s*[·\u00b7]\s*", raw)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                name = re.sub(r"[\d,\s]+$", "", part).strip()
                if (re.match(r"^[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+"
                             r"(\s+[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+)+$", name)
                        and len(name) < 60 and not re.search(r"\d", name)):
                    authors.append(Author(name=name))
            if authors:
                return authors
            continue

        # DOCX multi-line block
        if "\n" in block:
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if not lines:
                continue
            first = lines[0]
            if (re.match(r"^[\w\u00C0-\u024F][\w\u00C0-\u024F\-]*"
                         r"(\s+[\w\u00C0-\u024F][\w\u00C0-\u024F\-]*)+$", first)
                    and len(first) < 60 and not re.search(r"\d", first)):
                author = Author(name=first)
                for line in lines[1:]:
                    low = line.lower()
                    if "@" in line:
                        author.email = line
                    elif re.match(r"^(dept|department|school|faculty|division)", low):
                        author.department = line
                    elif re.search(r"(university|institute|college|laboratory|lab\b)", low):
                        author.organization = line
                    elif re.match(r"^[A-Z\u00C0-\u024F][a-z\u00C0-\u024F\s]+,\s*\S", line):
                        author.city = line
                    elif not author.organization:
                        author.organization = line
                authors.append(author)
            continue

        # PDF single-line name
        if not authors:
            line = block.strip()
            if re.match(r"^[A-Z][a-z]+([\s\-][A-Z][a-z]+)+$", line):
                author = Author(name=line)
                found_idx = blocks.index(block)
                for aff_block in blocks[found_idx + 1:found_idx + 7]:
                    if "\n" in aff_block or _detect_heading(aff_block):
                        break
                    aff = aff_block.strip()
                    low = aff.lower()
                    if "@" in aff:
                        author.email = aff
                    elif re.match(r"^(dept|department|school|faculty|division)", low):
                        author.department = aff
                    elif re.search(r"(university|institute|college|laboratory|lab\b)", low):
                        author.organization = aff
                    elif re.match(r"^[A-Z][a-zA-Z\s]+,\s*[A-Z]{2}\b", aff):
                        author.city = aff
                    elif not author.organization:
                        author.organization = aff
                authors.append(author)
                break

    return authors


# ─────────────────────────────────────────────────────────────────────────────
# Body parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_body(blocks: list, doc: Document):
    current_heading = None
    current_body    = []
    next_is_abstract = False
    in_references    = False

    author_names = {a.name for a in doc.authors}

    def _flush():
        if current_heading and current_body:
            doc.sections.append(Section(
                heading=current_heading,
                body="\n\n".join(current_body).strip()
            ))

    for block in blocks:
        lower      = block.lower().strip()
        first_line = block.split("\n")[0].strip()

        # Skip title / author identity blocks
        if first_line == doc.title or first_line in author_names:
            continue

        # Image marker
        if block.startswith("__IMAGE__"):
            img_path = re.search(r"__IMAGE__(.*?)__PAGE_", block)
            if img_path and current_heading is not None:
                current_body.append(f"__PENDING_IMG__{img_path.group(1)}")
            continue

        # Figure caption — pair with pending image
        if re.match(r"^(fig\.?|figure)\s*\d+", lower):
            caption = block.strip()
            for i in range(len(current_body) - 1, -1, -1):
                if current_body[i].startswith("__PENDING_IMG__"):
                    img_path = current_body[i][len("__PENDING_IMG__"):]
                    fig_tex  = _build_figure(img_path, caption)
                    current_body[i] = "%%RAWTEX%%" + fig_tex + "%%ENDRAWTEX%%"
                    break
            continue

        # Abstract
        if lower == "abstract":
            next_is_abstract = True
            continue
        if next_is_abstract:
            doc.abstract     = block.strip()
            next_is_abstract = False
            continue
        if re.match(r"^abstract[\s:—\-]+\S", block, re.IGNORECASE) and not doc.abstract:
            doc.abstract = re.sub(r"^abstract[\s:—\-]+", "", block, flags=re.IGNORECASE).strip()
            continue

        # Keywords
        if re.match(r"^(keywords?|index terms?)[\s:—\-]", lower):
            kw = re.sub(r"^(keywords?|index terms?)[\s:—\-]+", "", block, flags=re.IGNORECASE)
            doc.keywords = [k.strip() for k in re.split(r"[;,]", kw) if k.strip()]
            continue

        # References section
        if re.match(r"^references\.?$", lower):
            _flush()
            current_heading = None
            current_body    = []
            in_references   = True
            continue

        if in_references:
            _accumulate_reference(block, doc)
            continue

        # Display math
        if block.startswith("$$") and block.endswith("$$"):
            if current_heading is not None:
                inner = block[2:-2].strip()
                current_body.append(
                    "%%RAWTEX%%\\begin{equation}\n" + inner + "\n\\end{equation}%%ENDRAWTEX%%"
                )
            continue

        # Section heading
        heading = _detect_heading(block)
        if heading:
            _flush()
            current_heading = heading
            current_body    = []
            continue

        # Body text — NOTE: no table re-detection here.
        # Tables are injected by _inject_tables() after parse completes.
        if current_heading is not None:
            current_body.append(block.strip())

    _flush()


# ─────────────────────────────────────────────────────────────────────────────
# References
# ─────────────────────────────────────────────────────────────────────────────

def _accumulate_reference(block: str, doc: Document):
    """
    Add block content to doc.references as Reference objects.

    Each call handles one pdfplumber/PyMuPDF text block. The key insight is
    that a single [N] reference entry is often split across MULTIPLE blocks
    because double-newlines inside the PDF break the entry into fragments:

        block 1: "[1] Berson et al. URSA: A unified resource allocator"
        block 2: "for registers and functional units in VLIW..."
        block 3: "Architectures and Compilation Techniques..., January 1993."
        block 4: "[2] Bradlee. Retargetable Instruction Scheduling..."

    Rule:
      - Block starts with [N]  → new Reference entry
      - Block has NO [N] prefix → continuation of previous entry; always append

    Also handles blocks containing multiple [N] entries on one line.
    Also handles bare-number format: "1." or "1 " at line start (Springer).
    """
    block = block.strip()
    if not block:
        return

    # Split on any [N] or bare "N." markers within the block
    # Matches: [1], [12], 1. Author..., 12. Author...
    REF_START = re.compile(r"(?=\[\d+\])|(?=^\d+\.\s)", re.MULTILINE)
    parts = REF_START.split(block)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Try [N] prefix
        idx_match = re.match(r"^\[(\d+)\]\s*", part)
        # Try bare "N." prefix (Springer/numbered format)
        if not idx_match:
            idx_match = re.match(r"^(\d+)\.\s+", part)

        if idx_match:
            # This part starts a new reference
            idx  = int(idx_match.group(1))
            text = part[idx_match.end():].strip()
            if text:
                doc.references.append(Reference(index=idx, text=text))
        else:
            # No [N] prefix — this is a continuation line of the previous ref
            # Always append to previous; never create a new ref from a bare fragment
            if doc.references:
                doc.references[-1].text += " " + part


# ─────────────────────────────────────────────────────────────────────────────
# Heading detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_heading(block: str):
    stripped = block.strip()
    lines    = stripped.split("\n")

    # Two-line: "1\nIntroduction"
    if len(lines) == 2:
        num, text = lines[0].strip(), lines[1].strip()
        if re.match(r"^\d+(\.\d+)*\.?$|^[ivxlcdmIVXLCDM]+\.?$", num, re.IGNORECASE):
            clean = _LEADING_NUM.sub("", text).strip().rstrip(".")
            if clean.lower() in IEEE_SECTION_NAMES or (text.isupper() and len(text) < 60):
                return clean
            if len(clean) < 60 and not re.search(r"[.!?]", clean):
                return clean

    if len(stripped) <= 80:
        m = re.match(r"^([IVXLCDM]+)\.\s+(.+)$", stripped)
        if m and len(stripped) < 60:
            return m.group(2).title() if m.group(2).isupper() else m.group(2)

        m = re.match(r"^([A-Z])\.\s+(.+)$", stripped)
        if m and len(stripped) < 80 and not re.search(r"[.!?]$", stripped):
            return m.group(2)

        clean = _LEADING_NUM.sub("", stripped).strip().rstrip(".")
        if clean.lower() in IEEE_SECTION_NAMES:
            return clean
        if stripped.isupper() and 3 < len(stripped) < 60:
            return stripped.title()

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Figure builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_figure(img_path: str, caption: str) -> str:
    import os as _os
    from core.config import ROOT
    from stages.normalizer import _fix_math_symbols, _fix_unicode_spaces, _strip_control_chars

    def esc(t):
        t = _strip_control_chars(t)
        t = _fix_unicode_spaces(t)
        t = _fix_math_symbols(t)
        t = re.sub(r"\s*\n\s*", " ", t).strip()
        parts = re.split(r"(\$[^$]*\$)", t)
        result = []
        for p in parts:
            if p.startswith("$") and p.endswith("$"):
                result.append(p)
            else:
                result.append(
                    p.replace("_", r"\_").replace("%", r"\%")
                     .replace("&", r"\&").replace("#", r"\#")
                )
        return "".join(result)

    abs_path  = _os.path.join(ROOT, img_path).replace("\\", "/")
    num_match = re.search(r"(\d+)", caption)
    label     = f"fig{num_match.group(1)}" if num_match else "fig"

    return (
        r"\begin{figure}[h!]" + "\n"
        r"\centering" + "\n"
        r"\includegraphics[width=\columnwidth]{" + abs_path + "}\n"
        r"\caption{" + esc(caption) + "}\n"
        r"\label{" + label + "}\n"
        r"\end{figure}"
    )


def _extract_references_from_text(raw: str) -> list:
    """
    Standalone reference extractor — works directly on raw text.
    Used as fallback when block-by-block accumulator fails, and by AI detector.

    Finds the references section then splits on [N] or N. markers.
    Handles multi-line entries spanning several text lines.
    Returns list[Reference].
    """
    # Find start of references section
    ref_start = None
    for pattern in [r"\nReferences\s*\n", r"\nBibliography\s*\n", r"\nREFERENCES\s*\n"]:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            ref_start = m.end()
            break

    if ref_start is None:
        m = re.search(r"(?:^|\n)\[1\]", raw)
        if m:
            ref_start = m.start()

    if ref_start is None:
        return []

    ref_text = raw[ref_start:]
    ref_text = ref_text.replace("\r\n", "\n").replace("\r", "\n")

    lines        = ref_text.split("\n")
    refs         = []
    current_idx  = None
    current_text = []

    def _flush():
        if current_idx is not None and current_text:
            text = " ".join(current_text).strip()
            text = re.sub(r"\s{2,}", " ", text)
            refs.append(Reference(index=current_idx, text=text))

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # [N] format
        m = re.match(r"^\[(\d+)\]\s*(.*)", line)
        if not m:
            # Bare "N." format (Springer)
            m = re.match(r"^(\d+)\.\s+(.*)", line)

        if m:
            _flush()
            current_idx  = int(m.group(1))
            text_start   = m.group(2).strip()
            current_text = [text_start] if text_start else []
        elif current_idx is not None:
            current_text.append(line)

    _flush()
    return refs