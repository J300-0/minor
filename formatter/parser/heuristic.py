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
from core.models import Document, Author, Section, Reference, Table, Figure
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
    images = rich.get("images", [])
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
        _parse_font_aware(blocks, tables, images, doc)
    else:
        _parse_text_only(blocks, tables, images, doc)

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

def _parse_font_aware(blocks, tables, images, doc):
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
    _attach_images(doc, images, blocks)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# MODE B: Text-only parsing (pdfplumber fallback, line-per-block, no font info)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_text_only(blocks, tables, images, doc):
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

    # ── Title: scan first ~30 lines, skip metadata/journal headers ─────────
    # The title is typically a prominent line before "Abstract",
    # not a journal header, author line, or URL.
    doc.title = lines[0]  # fallback
    idx = 1
    best_title_idx = 0
    best_title_score = 0
    abstract_limit = min(total, 30)
    for i in range(abstract_limit):
        low = lines[i].lower().strip()
        if low.startswith("abstract"):
            break
        line = lines[i].strip()
        # Skip lines that look like journal/page metadata
        if _is_metadata_line(line):
            continue
        # Skip empty / too short lines
        if len(line) < 5:
            continue
        # Score: prefer lines that look like a title
        if (not re.search(r"@|\.\w{2,3}$", line)
                and not re.match(r"^https?://", line)
                and not re.match(r"^\d+$", line)
                and not re.match(r"^\[\d+\]", line)):
            score = len(line)
            # Penalize lines with commas (author lists), interpuncts (author separators)
            score -= line.count(",") * 15
            score -= line.count("·") * 20
            # Penalize lines with digits (affiliations like "Yu1,2")
            digit_count = sum(1 for c in line if c.isdigit())
            score -= digit_count * 5
            # Penalize Received/Revised/Accepted lines
            if re.match(r"^(Received|Revised|Accepted)", line, re.IGNORECASE):
                score -= 100
            # Boost lines that look like a proper title (mixed case, no trailing comma)
            if line[0].isupper() and not line.endswith(","):
                score += 10
            if score > best_title_score:
                best_title_score = score
                best_title_idx = i
                doc.title = line
    idx = best_title_idx + 1

    # ── Header zone: everything until "Abstract" = author/affiliation ─────────
    current_author = None
    while idx < total:
        low = lines[idx].lower().strip()
        if (low == "abstract" or low.startswith("abstract:") or
            low.startswith("abstract—") or low.startswith("abstract.")):
            break
        line = lines[idx].strip()
        # Skip metadata lines in header zone
        if _is_metadata_line(line):
            idx += 1
            continue
        # Skip reference-like lines in header zone
        if re.match(r"^\[\d+\]", line):
            idx += 1
            continue
        # Check for multiple authors on one line separated by · or "and"
        # e.g. "Haolin Yu1,2 · Kaiyang Guo3 · Mahdi Karami1"
        if "·" in line or (" and " in line and _has_multi_author_pattern(line)):
            parts = re.split(r"\s*[·]\s*|\s+and\s+", line)
            for part in parts:
                part = part.strip()
                if part and _looks_like_author(part):
                    current_author = Author(name=_clean_author_name(part))
                    doc.authors.append(current_author)
            idx += 1
            continue
        # Check if it looks like an author name
        if _looks_like_author(line):
            current_author = Author(name=_clean_author_name(line))
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
        abs_parts = []
        if m:
            abs_parts.append(m.group(1).strip())
        idx += 1

        # Collect continuation lines until we hit a heading or keywords line
        # (the abstract may span multiple extracted lines due to PDF column wrapping)
        while idx < total:
            l = lines[idx]
            # Stop at headings
            if _is_heading_line(l):
                break
            # Stop at keywords line
            if re.match(r"^(?:keywords?|index terms?)\b", l, re.IGNORECASE):
                break
            # Stop at obvious section starts
            if re.match(r"^1[\s.]+[A-Z]", l):  # "1 Introduction" or "1. Introduction"
                break
            abs_parts.append(l)
            idx += 1

        if abs_parts:
            doc.abstract = " ".join(abs_parts).strip()

    # ── Keywords (if present) ─────────────────────────────────────────────────
    if idx < total:
        m = re.match(r"^(?:keywords?|index terms?)[\s:—\-]+(.+)", lines[idx], re.IGNORECASE)
        if m:
            doc.keywords = [k.strip() for k in re.split(r"[;,·]", m.group(1)) if k.strip()]
            idx += 1

    # ── Sections: walk remaining lines ────────────────────────────────────────
    cur_head  = None
    cur_body  = []
    in_refs   = False
    in_appendix = False  # once True, stop adding new sections

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

        # Skip metadata / page header lines (e.g. "Page 5 of 36 ...")
        if _is_metadata_line(line):
            continue

        # Table caption or table data lines → skip (pdfplumber handles tables)
        if re.match(r"^Table\s+\d+", line):
            continue
        if _is_table_data_line(line):
            continue

        # Detect appendix start — catches "Appendix A: Title", "Appendix B: ..."
        # Must be a standalone heading, NOT a body sentence.
        # Appendix section headers use a colon: "Appendix A: Privacy Risks..."
        # Body references use a period: "Appendix A. We note that..."
        # Require a colon after the letter, OR the word "Appendix" alone on the line.
        if re.match(r"^Appendix\s+[A-Z]\s*:", line) or \
           re.match(r"^Appendix\s*$", line, re.IGNORECASE):
            flush()
            in_appendix = True
            cur_head = None
            cur_body = []
            continue

        # Skip everything past the appendix marker
        if in_appendix:
            continue

        # Is this line a section heading?
        if _is_heading_line(line):
            flush()
            cur_head = _strip_num(line)
            cur_body = []
            continue
        if cur_head is not None:
            cur_body.append(line)
        # Lines before first heading (after abstract) — start a section
        elif not doc.sections and not in_refs:
            # If we haven't found a heading yet, these are continuation
            # of abstract or pre-section text. Try to detect heading.
            pass

    flush()
    _attach_tables(doc, tables)
    _attach_images(doc, images, blocks)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _is_metadata_line(line: str) -> bool:
    """Detect journal/page metadata lines that should be skipped."""
    line = line.strip()
    if not line:
        return True
    # "Page X of Y ..." anywhere in the line
    if re.search(r"Page\s+\d+\s+of\s+\d+", line, re.IGNORECASE):
        return True
    # Journal header like "Machine Learning (2026) 115:72" anywhere
    if re.search(r"\(\d{4}\)\s+\d+:\d+", line):
        return True
    # "et al. [...]" artifact
    if line.startswith("et al."):
        return True
    # DOI / URL lines
    if re.match(r"^https?://", line) or "doi.org" in line.lower():
        return True
    # Received/Revised/Accepted dates (also combined like "Received: ... / Revised: ...")
    if re.match(r"^(Received|Revised|Accepted|Published)\s*:", line, re.IGNORECASE):
        return True
    if re.match(r"^Received\s*:", line) or ("Received:" in line and "Accepted:" in line):
        return True
    # Copyright / Creative Commons license lines
    if "©" in line or "copyright" in line.lower():
        return True
    if "creative commons" in line.lower() or "cc by" in line.lower():
        return True
    if re.search(r"\b(CC\s+BY|Attribution)\b.*licen", line, re.IGNORECASE):
        return True
    # Lines that are just a small number (page number artifact like "72")
    if re.match(r"^\d{1,4}$", line):
        return True
    # Lines starting with a small number then "Page" (e.g. "72 Page 2 of 36 ...")
    if re.match(r"^\d{1,4}\s+Page\s+\d+", line, re.IGNORECASE):
        return True
    return False


def _is_heading_line(line: str) -> bool:
    """Is this single line a section/subsection heading?"""
    line = line.strip()
    if not line or len(line) > 100:
        return False
    # Skip metadata/page-header lines — never treat them as headings
    if _is_metadata_line(line):
        return False
    # Skip lines that are mostly math symbols / too short to be real headings
    # e.g. "?N ·", "· ?GP · ·", "| · ?N"
    alpha_chars = sum(1 for c in line if c.isalpha())
    if alpha_chars < 3:
        return False
    # Numbered: "1 Introduction", "2.1 Subsection", "3 Experimental Study"
    m = _HEADING_RE.match(line)
    if m:
        heading_text = m.group(3).strip()
        # Reject if the heading text looks like a sentence (too long or starts
        # with a common English subject word like "We", "The", "In", "To", "A")
        _SENTENCE_STARTERS = {"we", "the", "in", "to", "a", "an", "it", "this",
                               "our", "these", "that", "there", "if", "for"}
        first_word = heading_text.split()[0].lower().rstrip(",;:") if heading_text else ""
        if len(heading_text) <= 70 and first_word not in _SENTENCE_STARTERS:
            return True
    # Known name alone on a line
    if line.lower().rstrip(".") in _SECTION_NAMES:
        return True
    # ALL CAPS short: "INTRODUCTION" — must be only letters and spaces, at least 2 words
    # or a single known acronym-like word (pure alpha, >= 4 chars)
    if (line.isupper()
            and re.match(r"^[A-Z][A-Z\s]+$", line)   # only uppercase letters + spaces
            and 3 < len(line) < 60):
        words = line.split()
        if len(words) >= 2 or (len(words) == 1 and len(line) >= 4):
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
    """Heuristic: 2-5 words, starts with capital, allows superscript digits."""
    text = text.strip()
    if not text or len(text) > 80:
        return False
    if "@" in text:
        return False
    # Strip trailing markers like *, †, and superscript digits (e.g. "Yu1,2")
    clean = re.sub(r"[*†‡§\d,·]+$", "", text).strip()
    # Also strip superscript digits attached to names mid-string
    clean = re.sub(r"\d+", "", clean).strip()
    # Strip interpunct separators common in author lists
    clean = re.sub(r"\s*[·]\s*", " ", clean).strip()
    words = [w for w in clean.split() if w]
    if not 2 <= len(words) <= 5:
        return False
    if not all(w[0].isupper() for w in words if w):
        return False
    # ALL CAPS words = likely a title or heading, not a name
    if all(w.isupper() for w in words):
        return False
    # Exclude common non-name patterns
    low = text.lower()
    if any(kw in low for kw in ["department", "university", "institute", "school",
                                 "college", "inc", "corp", "lab", "formula",
                                 "abstract", "introduction", "conclusion",
                                 "received:", "accepted:", "revised:"]):
        return False
    return True


def _clean_author_name(text: str) -> str:
    """Strip superscript digits, markers, etc. from author name."""
    clean = re.sub(r"[*†‡§]+", "", text).strip()
    clean = re.sub(r"\d+,?\d*$", "", clean).strip()  # trailing "1,2"
    clean = re.sub(r"(\w)\d+", r"\1", clean)  # inline "Yu1" -> "Yu"
    return clean.strip()


def _has_multi_author_pattern(line: str) -> bool:
    """Check if line has multiple author-like names separated by 'and'."""
    parts = re.split(r"\s+and\s+", line)
    return len(parts) >= 2 and all(_looks_like_author(p.strip()) for p in parts if p.strip())


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


def _extract_tables_from_text(blocks: list) -> list:
    """Extract tables from raw text blocks when pdfplumber finds none.

    Looks for 'Table N' caption lines followed by rows of space-separated data.
    Returns list of dicts: {caption, headers, rows, notes, page}.
    """
    tables = []
    i = 0
    while i < len(blocks):
        t = blocks[i]["text"].strip()
        page = blocks[i].get("page", 1)

        # Detect "Table N" caption
        m = re.match(r"^Table\s+(\d+)\b\s*(.*)", t)
        if not m:
            i += 1
            continue

        table_num = int(m.group(1))
        caption_parts = [m.group(2).strip()] if m.group(2).strip() else []
        i += 1

        # Collect multi-line caption (text lines before data starts)
        while i < len(blocks):
            t2 = blocks[i]["text"].strip()
            # Skip arrows/symbols that are caption continuation
            if re.match(r'^[⇑⇓↑↓\s,]+$', t2):
                i += 1
                continue
            # Is this data? (mostly numbers or known method names)
            if _looks_like_table_row(t2):
                break
            # Still caption text
            caption_parts.append(t2)
            i += 1

        caption = " ".join(caption_parts).strip()
        # Clean up caption artifacts
        caption = re.sub(r'\s+', ' ', caption)

        # Now collect data rows
        headers = []
        rows = []
        while i < len(blocks):
            t2 = blocks[i]["text"].strip()
            # Skip metadata lines, empty-ish lines
            if _is_metadata_line(t2):
                i += 1
                continue
            if re.match(r'^[⇑⇓↑↓\s,]+$', t2):
                i += 1
                continue
            # Stop at next "Table N" or section heading or references
            if re.match(r'^Table\s+\d+\b', t2):
                break
            if _is_heading_line(t2):
                break
            if re.match(r'^references\.?\s*$', t2, re.IGNORECASE):
                break
            # Blank or very short non-data line = end of table
            if len(t2) < 3:
                i += 1
                continue

            if _looks_like_table_row(t2):
                cells = _split_table_row(t2)
                if not headers:
                    headers = cells
                else:
                    rows.append(cells)
                i += 1
            else:
                # Could be a sub-label like "ours" or continuation
                # If very short (1-2 words), skip it
                if len(t2.split()) <= 2:
                    i += 1
                    continue
                break

        if headers and rows:
            tables.append({
                "caption": caption,
                "headers": headers,
                "rows": rows,
                "notes": "",
                "page": page,
            })
            log.info("Text-based table extraction: Table %d with %d rows", table_num, len(rows))

    log.info("Extracted %d tables from text", len(tables))
    return tables


def _looks_like_table_row(line: str) -> bool:
    """Check if a line looks like a table data row (mix of labels and numbers)."""
    line = line.strip()
    if not line:
        return False
    tokens = line.split()
    if len(tokens) < 2:
        return False
    # Count numeric-like tokens (numbers, possibly with arrows/symbols)
    num_tokens = sum(1 for t in tokens if re.match(r'^[\d.⇑⇓↑↓±\-]+$', t))
    # A table row has at least some numeric tokens
    if num_tokens >= 2:
        return True
    # Column header line: multiple short text tokens
    if len(tokens) >= 3 and all(len(t) < 15 for t in tokens):
        # Check it's not a regular sentence (no common verbs/prepositions)
        low = line.lower()
        sentence_words = {"the", "is", "are", "was", "were", "have", "has", "been",
                          "with", "that", "this", "from", "which", "where", "when"}
        if not any(w in low.split() for w in sentence_words):
            return True
    return False


def _split_table_row(line: str) -> list:
    """Split a table row into cells. Uses 2+ spaces as separator when possible."""
    line = line.strip()
    # Try splitting on 2+ spaces first (common in text-extracted tables)
    parts = re.split(r'\s{2,}', line)
    if len(parts) >= 2:
        return [p.strip() for p in parts if p.strip()]
    # Fallback: single space split
    return line.split()


def _attach_tables(doc, tables_raw: list, blocks: list = None):
    """Attach extracted tables to sections.

    If tables_raw is empty and blocks are provided, attempts text-based
    table extraction as fallback.
    """
    if not tables_raw and blocks:
        tables_raw = _extract_tables_from_text(blocks)

    if not tables_raw or not doc.sections:
        return

    for tbl_raw in tables_raw:
        headers = [str(c or "").strip() for c in tbl_raw.get("headers", [])]
        rows    = [[str(c or "").strip() for c in row] for row in tbl_raw.get("rows", [])]
        caption = tbl_raw.get("caption", "")
        notes   = tbl_raw.get("notes", "")
        tbl_page = tbl_raw.get("page", 0)

        tbl = Table(caption=caption, headers=headers, rows=rows, notes=notes)

        # Try to attach by page proximity or by table mention in section body
        attached = False

        # First try: match by "Table N" reference in section body
        # Extract table number from caption
        tbl_num_m = re.match(r'^.*?Table\s*(\d+)', caption) if caption else None
        tbl_ref = f"table {tbl_num_m.group(1)}" if tbl_num_m else None

        if tbl_ref:
            for section in doc.sections:
                if tbl_ref in section.body.lower():
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


def _attach_images(doc, images_raw: list, blocks: list):
    """Attach extracted images to sections based on page numbers and figure captions."""
    if not images_raw or not doc.sections:
        return

    # Build a mapping: page_num → section index
    # We approximate by distributing pages across sections
    section_pages = {}
    if blocks:
        for i, sec in enumerate(doc.sections):
            # Find which page this section's heading appears on
            for b in blocks:
                if b["text"].strip() == sec.heading or sec.heading in b["text"]:
                    section_pages[i] = b.get("page", 1)
                    break

    # Detect figure captions from text blocks (e.g., "Fig. 1 ...", "Figure 1 ...")
    fig_captions = {}
    for b in blocks:
        t = b["text"].strip()
        m = re.match(r"^(?:Fig\.?|Figure)\s*(\d+)\b\s*(.*)", t, re.IGNORECASE)
        if m:
            fig_num = int(m.group(1))
            caption_text = m.group(2).strip()
            page = b.get("page", 1)
            fig_captions[fig_num] = {"caption": caption_text, "page": page}

    for idx, img in enumerate(images_raw):
        img_page = img.get("page", 1)
        fig_num = idx + 1
        caption = ""

        # Try to match with a figure caption
        if fig_num in fig_captions:
            caption = fig_captions[fig_num].get("caption", "")

        fig = Figure(
            caption=caption,
            image_path=img.get("path", ""),
            label=f"fig:{fig_num}",
        )

        # Attach to the section closest to the image's page
        best_section = doc.sections[-1]
        best_dist = float("inf")
        for i, sec in enumerate(doc.sections):
            sec_page = section_pages.get(i, 1)
            dist = abs(sec_page - img_page)
            if dist < best_dist:
                best_dist = dist
                best_section = sec

        best_section.figures.append(fig)

    log.info("Attached %d image(s) to sections", len(images_raw))


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

    # Try [N] style first
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

    # If [N] style found nothing, try author-year style
    # Pattern: lines starting with "Author, A." or "Author, A.," etc.
    if not doc.references:
        # Join lines and split on what looks like new reference entries
        # Author-year refs typically start with a capitalized surname
        # e.g. "Achituve, I., Sharma, S., ..."
        # Split on newlines that start a new author entry
        current_ref = []
        ref_idx = 0
        for line in ref_lines:
            line = line.strip()
            if not line:
                continue
            # Skip metadata/page header lines in reference section
            if _is_metadata_line(line):
                continue
            # A new reference entry typically starts with a capital letter
            # followed by author name patterns, and the previous entry
            # likely ended with a year, page number, or URL
            is_new_entry = (
                re.match(r"^[A-Z][a-z]+,?\s", line) and
                len(line) > 10 and
                # Previous ref should have some content
                len(current_ref) > 0
            )
            if is_new_entry:
                # Flush previous reference
                ref_text_joined = " ".join(current_ref).strip()
                if ref_text_joined and len(ref_text_joined) > 15:
                    ref_idx += 1
                    doc.references.append(Reference(index=ref_idx, text=ref_text_joined))
                current_ref = [line]
            else:
                current_ref.append(line)
        # Flush last reference
        if current_ref:
            ref_text_joined = " ".join(current_ref).strip()
            if ref_text_joined and len(ref_text_joined) > 15:
                ref_idx += 1
                doc.references.append(Reference(index=ref_idx, text=ref_text_joined))

    # Strip author-affiliation info that sometimes appears after the last reference
    # in Springer papers (extended author info block contains emails, institutions)
    clean_refs = []
    for r in doc.references:
        text = r.text
        # Drop if it contains an email address
        if re.search(r"[\w.+-]+@[\w.-]+\.\w+", text):
            continue
        # Drop if it looks like an author name list (contains · interpuncts)
        if "·" in text or "$\\cdot$" in text:
            continue
        # Drop Springer "Authors and Affiliations" block header
        if re.match(r"^Authors?\s+and\s+Affiliations?", text, re.IGNORECASE):
            continue
        # Drop publisher/license boilerplate (Springer, Elsevier end-of-paper text)
        if re.search(r"\b(Springer Nature|Elsevier)\b.*licen", text, re.IGNORECASE):
            continue
        if re.match(r"^Open Access\b", text, re.IGNORECASE):
            continue
        # Drop if very short and looks like affiliation only
        if re.search(r"\b(University|Institute|Lab|Huawei|Google|Microsoft|DeepMind)\b",
                     text) and len(text.split()) < 20:
            continue
        clean_refs.append(r)
    doc.references = clean_refs

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
