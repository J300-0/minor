"""
parser/heuristic.py — Stage 2: Convert extracted blocks into a Document.

Two modes:
  A. Font-aware (pymupdf): uses font_size to detect headings (threshold > body)
  B. Text-only (pdfplumber fallback): uses regex patterns for numbered headings

In both modes:
  - Title     = first block / largest font on page 1
  - Abstract  = block(s) after "Abstract" label
  - Authors   = blocks between title and abstract
  - Sections  = headed by detected headings
  - References= text after "References" label, split on [N] markers
"""
import re
from statistics import median
from core.models import Document, Author, Section, Reference, Table
from core.logger import get_logger

log = get_logger(__name__)

# ── Known section names ───────────────────────────────────────────────────────
_SECTION_NAMES = {
    "abstract", "introduction", "related work", "related works",
    "background", "preliminaries", "motivation", "overview",
    "methodology", "method", "methods", "approach", "proposed method",
    "framework", "architecture", "system design", "implementation",
    "experiments", "experiment", "experimental setup", "experimental study",
    "experimental results", "evaluation", "results", "results and discussion",
    "analysis", "ablation", "ablation study", "discussion",
    "conclusion", "conclusions", "future work", "future directions",
    "limitations", "acknowledgment", "acknowledgements",
    "references", "bibliography", "appendix",
    "dataset", "datasets", "contributions", "literature review",
}

# Matches heading patterns:  "1 Title", "1. Title", "1.2 Title", "II. Title"
_HEADING_RE = re.compile(
    r"^(?:"
    r"(\d+(?:\.\d+)*\.?)\s+"          # "1 " / "1. " / "2.1 "
    r"|([IVXLCDM]{1,6})[.\s]+"        # "II. " / "IV "
    r")"
    r"([A-Z].*)$"                      # rest = heading text (starts with capital)
)


def parse(rich: dict) -> Document:
    blocks = rich.get("blocks", [])
    tables = rich.get("tables", [])
    doc    = Document()

    if not blocks:
        raw = rich.get("raw_text", "")
        blocks = [{"text": l.strip(), "font_size": 10, "bold": False, "page": 1}
                  for l in raw.split("\n") if l.strip()]

    if not blocks:
        log.error("No content extracted")
        return doc

    # Decide mode: if all font_size == 10 and all bold == False → text-only mode
    has_font_info = any(b["font_size"] != 10 or b["bold"] for b in blocks)

    if has_font_info:
        _parse_font_aware(blocks, tables, doc)
    else:
        _parse_text_only(blocks, tables, doc)

    # Remove any section headed "Abstract" — template handles abstract separately
    doc.sections = [s for s in doc.sections if s.heading.lower().rstrip(".") != "abstract"]

    # Deduplicate sections (same heading + same body)
    seen = set()
    unique_sections = []
    for s in doc.sections:
        key = (s.heading.lower(), s.body[:200])
        if key not in seen:
            seen.add(key)
            unique_sections.append(s)
    doc.sections = unique_sections

    # Fallback
    if not doc.sections:
        body = "\n".join(b["text"] for b in blocks if b["text"] != doc.title)
        doc.sections.append(Section(heading="Content", body=body.strip()))

    log.info("parse: title=%r  authors=%d  sections=%d  refs=%d",
             doc.title[:40] if doc.title else "?",
             len(doc.authors), len(doc.sections), len(doc.references))
    return doc


# ══════════════════════════════════════════════════════════════════════════════
# MODE A: Font-aware parsing (pymupdf blocks with font_size + bold)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_font_aware(blocks, tables, doc):
    sizes = [b["font_size"] for b in blocks if b["font_size"] > 0]
    body_size = median(sizes) if sizes else 10
    threshold = body_size + 1.5

    # Title = largest font on page 1
    page1 = [b for b in blocks if b["page"] <= 1] or blocks[:5]
    if page1:
        title_b = max(page1, key=lambda b: b["font_size"])
        if title_b["font_size"] >= body_size:
            doc.title = title_b["text"].strip()

    abstract_block_texts = _detect_abstract_keywords(blocks, doc)
    _detect_authors_between_title_and_abstract(blocks, doc)

    # Sections — skip title, all abstract blocks, and author blocks
    cur_head, cur_body, in_refs = None, [], False
    skip = {doc.title} | abstract_block_texts
    if doc.abstract:
        skip.add(doc.abstract)
    for a in doc.authors:
        if a.name:
            skip.add(a.name)

    def flush():
        if cur_head and cur_body:
            doc.sections.append(Section(heading=cur_head, body="\n\n".join(cur_body).strip()))

    for b in blocks:
        t = b["text"].strip()
        if not t or t in skip:
            continue
        if re.match(r"^abstract[\s:—\-]?$", t, re.IGNORECASE):
            continue
        if re.match(r"^(?:keywords?|index terms?)[\s:—\-]", t, re.IGNORECASE):
            continue
        if re.match(r"^references\.?\s*$", t, re.IGNORECASE):
            flush(); in_refs = True; continue
        if in_refs:
            continue

        # Skip table data lines (numeric rows, column headers, etc.)
        if _is_table_data_line(t):
            continue
        # Skip table caption lines
        if re.match(r"^Table\s+\d+", t):
            continue

        is_heading = (
            (b["font_size"] >= threshold and len(t) < 100)
            or (b["bold"] and len(t) <= 80 and not re.search(r"[.!?]$", t))
            or (_HEADING_RE.match(t) and len(t) <= 90)
            or (t.lower().rstrip(".") in _SECTION_NAMES and t.lower().rstrip(".") != "abstract")
        )

        # Don't create a section called "Abstract" — it's handled by the template
        if is_heading and t.lower().rstrip(".") == "abstract":
            continue

        if is_heading:
            flush(); cur_head = _strip_num(t); cur_body = []; continue
        if cur_head:
            cur_body.append(t)

    flush()
    _attach_tables(doc, tables)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# MODE B: Text-only parsing (pdfplumber fallback, line-per-block, no font info)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_text_only(blocks, tables, doc):
    """
    Each block is a single line of text with no font metadata.
    Strategy: walk lines sequentially. Detect header zone (title → abstract),
    then detect numbered headings, collect body lines, stop at References.
    """
    lines = [b["text"].strip() for b in blocks if b["text"].strip()]
    if not lines:
        return

    idx = 0
    total = len(lines)

    # ── Title: first line ─────────────────────────────────────────────────────
    doc.title = lines[0]
    idx = 1

    # ── Header zone: everything until "Abstract" = author/affiliation ─────────
    current_author = None
    while idx < total:
        low = lines[idx].lower().strip()
        if (low == "abstract" or low.startswith("abstract:") or
            low.startswith("abstract—") or low.startswith("abstract.")):
            break
        line = lines[idx].strip()
        # Skip reference-like lines in header zone
        if re.match(r"^\[\d+\]", line):
            idx += 1
            continue
        # Check if it looks like an author name
        if _looks_like_author(line):
            current_author = Author(name=line)
            doc.authors.append(current_author)
        elif current_author:
            # Try to attach affiliation info to the most recent author
            if _looks_like_department(line):
                current_author.department = line
            elif _looks_like_organization(line):
                current_author.organization = line
            elif _looks_like_location(line):
                current_author.city = line
            elif _looks_like_email(line):
                m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
                if m:
                    current_author.email = m.group(0)
        idx += 1

    # ── Abstract ──────────────────────────────────────────────────────────────
    if idx < total and lines[idx].lower().strip().startswith("abstract"):
        label = lines[idx]
        # Inline abstract: "Abstract This paper..." or "Abstract. This paper..."
        m = re.match(r"^abstract[\s:—.\-]+(.+)", label, re.IGNORECASE)
        if m:
            doc.abstract = m.group(1).strip()
        idx += 1

        # Multi-line abstract: collect lines until a numbered heading appears
        if not doc.abstract:
            abs_lines = []
            while idx < total:
                if _is_heading_line(lines[idx]):
                    break
                abs_lines.append(lines[idx])
                idx += 1
            doc.abstract = " ".join(abs_lines).strip()

    # ── Keywords (if present) ─────────────────────────────────────────────────
    if idx < total:
        m = re.match(r"^(?:keywords?|index terms?)[\s:—\-]+(.+)", lines[idx], re.IGNORECASE)
        if m:
            doc.keywords = [k.strip() for k in re.split(r"[;,·]", m.group(1)) if k.strip()]
            idx += 1

    # ── Sections: walk remaining lines ────────────────────────────────────────
    cur_head = None
    cur_body = []
    in_refs  = False

    def flush():
        if cur_head and cur_body:
            body = _join_body_lines(cur_body)
            if body:
                doc.sections.append(Section(heading=cur_head, body=body))

    while idx < total:
        line = lines[idx]
        idx += 1

        # References section → stop body, start reference parsing
        if re.match(r"^references\.?\s*$", line, re.IGNORECASE):
            flush()
            in_refs = True
            continue

        if in_refs:
            continue   # refs parsed separately below

        # Table caption or table data lines → skip (pdfplumber handles tables)
        if re.match(r"^Table\s+\d+", line):
            continue
        if _is_table_data_line(line):
            continue

        # Is this line a section heading?
        if _is_heading_line(line):
            flush()
            cur_head = _strip_num(line)
            cur_body = []
            continue

        # Regular body line
        if cur_head is not None:
            cur_body.append(line)
        # Lines before first heading (after abstract) — start a section
        elif not doc.sections and not in_refs:
            # If we haven't found a heading yet, these are continuation
            # of abstract or pre-section text. Try to detect heading.
            pass

    flush()
    _attach_tables(doc, tables)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_heading_line(line: str) -> bool:
    """Is this single line a section/subsection heading?"""
    line = line.strip()
    if not line or len(line) > 100:
        return False
    # Numbered: "1 Introduction", "2.1 Subsection", "3 Experimental Study"
    if _HEADING_RE.match(line):
        return True
    # Known name alone on a line
    if line.lower().rstrip(".") in _SECTION_NAMES:
        return True
    # ALL CAPS short: "INTRODUCTION"
    if line.isupper() and 3 < len(line) < 60 and len(line.split()) <= 6:
        return True
    return False


def _is_table_data_line(line: str) -> bool:
    """
    Detect lines that are table data (mostly numbers, column headers, etc.)
    These get extracted separately by pdfplumber and shouldn't be in body text.
    """
    line = line.strip()
    if not line:
        return False
    # Lines that are almost entirely numbers/dots/spaces (e.g. "1.56 1.02 1.48")
    non_digit = re.sub(r"[\d.\s\-]", "", line)
    if len(line) > 5 and len(non_digit) < len(line) * 0.3:
        return True
    # Short header-like labels followed by numbers
    # e.g. "loop1 1.56 1.02 1.48 1.01", "daxpy 1.17 1.00"
    if re.match(r"^\w+\s+[\d.]+(\s+[\d.]+){2,}", line):
        return True
    # Column header patterns like "RASSG REGOA RASSG REGOA"
    if re.match(r"^([A-Z]{3,}\s+){2,}", line):
        return True
    # Lines like "8 16", "long medium long medium"
    tokens = line.split()
    if all(re.match(r"^\d+$", t) for t in tokens) and len(tokens) <= 4:
        return True
    if all(t.lower() in ("long", "medium", "short") for t in tokens):
        return True
    # Table header/label lines
    # e.g. "Benchmark Number of Registers", "Speedup of fully cooperative..."
    if re.match(r"^(?:Benchmark|Speedup)\b", line, re.IGNORECASE):
        return True
    # Lines like "Livermore", "Clinpack", "SPEC", "pass" (table sub-labels)
    if line.lower() in ("livermore", "clinpack", "spec", "pass"):
        return True
    return False


def _strip_num(text: str) -> str:
    """Remove leading section number from heading text."""
    m = _HEADING_RE.match(text.strip())
    if m:
        return m.group(3).strip().rstrip(".")
    return text.strip().rstrip(".")


def _join_body_lines(lines: list) -> str:
    """
    Join consecutive body lines into paragraphs.
    Lines that end without sentence-ending punctuation are joined with a space
    (they were probably word-wrapped by the PDF extractor). Empty lines = paragraph break.
    """
    if not lines:
        return ""
    paragraphs = []
    current = []
    for line in lines:
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                current = []
        else:
            current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _looks_like_author(text: str) -> bool:
    """Heuristic: 2-5 words, starts with capital, no digits/email."""
    text = text.strip()
    if not text or len(text) > 60:
        return False
    if re.search(r"[\d@]", text):
        return False
    # Strip trailing markers like * (corresponding author)
    clean = re.sub(r"[*†‡§]+$", "", text).strip()
    words = clean.split()
    if not 2 <= len(words) <= 5:
        return False
    if not all(w[0].isupper() for w in words if w):
        return False
    # ALL CAPS words = likely a title or heading, not a name
    # Real names are "Peter SZABÓ" (mixed) or "Cindy Norris" (title case)
    if all(w.isupper() for w in words):
        return False
    # Exclude common non-name patterns
    low = text.lower()
    if any(kw in low for kw in ["department", "university", "institute", "school",
                                 "college", "inc", "corp", "lab", "formula",
                                 "abstract", "introduction", "conclusion"]):
        return False
    return True


def _looks_like_department(text: str) -> bool:
    """Heuristic: line contains department/school/faculty keywords."""
    low = text.lower().strip()
    return any(kw in low for kw in ["department", "dept", "faculty", "school of",
                                     "division of", "group of"])


def _looks_like_organization(text: str) -> bool:
    """Heuristic: line contains university/institute/company keywords."""
    low = text.lower().strip()
    return any(kw in low for kw in ["university", "institute", "college", "laboratory",
                                     "lab", "inc.", "corp", "ltd", "company",
                                     "research center", "polytechnic"])


def _looks_like_location(text: str) -> bool:
    """Heuristic: city/state/zip or country pattern."""
    text = text.strip()
    # "City, ST 12345" or "City, Country"
    if re.match(r"^[A-Z][a-z]+.*,\s*[A-Z]", text):
        return True
    return False


def _looks_like_email(text: str) -> bool:
    """Check if text contains an email address."""
    return bool(re.search(r"[\w.+-]+@[\w.-]+\.\w+", text.strip()))


def _detect_abstract_keywords(blocks, doc):
    """Detect abstract and keywords from blocks (font-aware mode).

    Collects ALL blocks after 'Abstract' label until the next heading
    (detected by font-size threshold or known heading pattern).
    Returns the set of block texts that belong to the abstract so they
    can be skipped during section parsing.
    """
    sizes = [b["font_size"] for b in blocks if b["font_size"] > 0]
    body_size = median(sizes) if sizes else 10
    threshold = body_size + 1.5
    abstract_block_texts = set()

    abs_collecting = False
    abs_parts = []

    for b in blocks:
        t = b["text"].strip()
        low = t.lower()

        # Keywords line — capture and stop abstract collection
        m_kw = re.match(r"^(?:keywords?|index terms?)[\s:—\-]+(.+)", t, re.IGNORECASE | re.DOTALL)
        if m_kw:
            if abs_collecting:
                abs_collecting = False
            doc.keywords = [k.strip() for k in re.split(r"[;,·]", m_kw.group(1)) if k.strip()]
            abstract_block_texts.add(t)
            continue

        # "Abstract" label alone on a line
        if low == "abstract" or re.match(r"^abstract[\s.]*$", low):
            abs_collecting = True
            abstract_block_texts.add(t)
            continue

        # Inline: "Abstract: This paper..." or "Abstract. This paper..."
        m_abs = re.match(r"^abstract[\s:—.\-]+(.+)", t, re.IGNORECASE | re.DOTALL)
        if m_abs and not doc.abstract:
            abs_parts.append(m_abs.group(1).strip())
            abs_collecting = True
            abstract_block_texts.add(t)
            continue

        # Collecting abstract blocks
        if abs_collecting:
            # Stop if this block looks like a heading
            is_heading = (
                (b["font_size"] >= threshold and len(t) < 100)
                or (b.get("bold", False) and len(t) <= 80 and not re.search(r"[.!?]$", t))
                or (_HEADING_RE.match(t) and len(t) <= 90)
                or (t.lower().rstrip(".") in _SECTION_NAMES and t.lower().rstrip(".") != "abstract")
            )
            if is_heading:
                abs_collecting = False
                # Don't add this block to abstract — it's the next section
            else:
                abs_parts.append(t)
                abstract_block_texts.add(t)
                continue

    if abs_parts and not doc.abstract:
        doc.abstract = " ".join(abs_parts).strip()

    return abstract_block_texts


def _detect_authors_between_title_and_abstract(blocks, doc):
    """Font-aware: blocks between title and abstract = potential author names + affiliations."""
    found_title = False
    current_author = None
    for b in blocks:
        t = b["text"].strip()
        if t == doc.title:
            found_title = True
            continue
        if not found_title:
            continue
        if re.match(r"^abstract", t, re.IGNORECASE):
            break
        if _looks_like_author(t):
            current_author = Author(name=t)
            doc.authors.append(current_author)
        elif current_author:
            if _looks_like_department(t):
                current_author.department = t
            elif _looks_like_organization(t):
                current_author.organization = t
            elif _looks_like_location(t):
                current_author.city = t
            elif _looks_like_email(t):
                m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", t)
                if m:
                    current_author.email = m.group(0)


def _attach_tables(doc, tables_raw: list):
    """Attach extracted tables to sections (to the last section before references)."""
    if not tables_raw or not doc.sections:
        return
    for tbl_raw in tables_raw:
        headers = [str(c or "").strip() for c in tbl_raw.get("headers", [])]
        rows    = [[str(c or "").strip() for c in row] for row in tbl_raw.get("rows", [])]
        caption = tbl_raw.get("caption", "")
        notes   = tbl_raw.get("notes", "")

        tbl = Table(caption=caption, headers=headers, rows=rows, notes=notes)

        # Attach to the most recent section (heuristic: tables appear after relevant text)
        # For a more accurate approach, we'd track page numbers
        attached = False
        for section in doc.sections:
            # If section body mentions this table, attach there
            low_body = section.body.lower()
            if "table" in low_body:
                section.tables.append(tbl)
                attached = True
                break
        if not attached:
            # Default: attach to last section before "Conclusions"
            target = doc.sections[-1]
            for s in doc.sections:
                if s.heading.lower() in ("conclusion", "conclusions", "future work"):
                    break
                target = s
            target.tables.append(tbl)

    log.info("Attached %d table(s) to sections", len(tables_raw))


def _extract_refs_from_blocks(blocks, doc):
    """Extract references from blocks that appear after 'References' heading."""
    # Find the references section in raw text
    in_refs = False
    ref_lines = []
    for b in blocks:
        t = b["text"].strip()
        if re.match(r"^references\.?\s*$", t, re.IGNORECASE):
            in_refs = True
            continue
        if in_refs:
            ref_lines.append(t)

    if not ref_lines:
        return

    ref_text = "\n".join(ref_lines)

    # Split on [N] patterns
    entries = re.split(r"(?=\[\d+\])", ref_text)
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        m = re.match(r"^\[(\d+)\]\s*(.+)", entry, re.DOTALL)
        if m:
            idx  = int(m.group(1))
            text = re.sub(r"\s+", " ", m.group(2)).strip()
            if text:
                doc.references.append(Reference(index=idx, text=text))

    # Deduplicate
    seen = set()
    unique = []
    for r in sorted(doc.references, key=lambda x: x.index):
        if r.index not in seen:
            seen.add(r.index)
            unique.append(r)
    doc.references = unique

    log.info("Extracted %d references", len(doc.references))


# ── Public exports ────────────────────────────────────────────────────────────

def extract_references(raw_text: str) -> list:
    """Standalone reference extraction from raw text."""
    doc = Document()
    blocks = [{"text": l.strip()} for l in raw_text.split("\n") if l.strip()]
    _extract_refs_from_blocks(blocks, doc)
    return doc.references
