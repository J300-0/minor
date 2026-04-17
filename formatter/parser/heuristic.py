"""
parser/heuristic.py — Font-aware + text-only heading/section detection.

Detects: title, authors, abstract, keywords, sections, references.
"""
import re
import logging
from typing import List, Optional

from core.models import Document, Author, Section, Table, Figure, Reference

log = logging.getLogger("paper_formatter")

# ── Patterns ─────────────────────────────────────────────────────

# Section heading patterns (numbered or keyword-based)
NUMBERED_HEADING_RE = re.compile(
    r"^(\d+\.?\d*\.?\d*\.?)\s+(.+)$"
)
KEYWORD_HEADINGS = {
    "abstract", "introduction", "background", "related work",
    "methodology", "method", "methods", "approach",
    "experiment", "experiments", "experimental", "results",
    "discussion", "evaluation", "analysis", "implementation",
    "conclusion", "conclusions", "summary",
    "acknowledgment", "acknowledgments", "acknowledgement", "acknowledgements",
    "future work",
}

# Metadata line patterns to filter
METADATA_PATTERNS = [
    re.compile(r"^\d+\s*$"),                          # page numbers
    re.compile(r"^\d+\s+\d+\s*$"),                    # "1 3" — split page numbers
    re.compile(r"^\d+\s{5,}"),                         # page number + running header ("4    Peter Szabó...")
    re.compile(r"^vol\.\s*\d+", re.I),                # volume numbers
    re.compile(r"^volume\s+[IVXLC\d]+", re.I),       # "Volume XXIV, ..."
    re.compile(r"^doi:\s*", re.I),                     # DOIs starting with DOI
    re.compile(r"DOI:\s*10\.\d+", re.I),              # DOI anywhere in line
    re.compile(r"^\d{4}\s+(IEEE|ACM|Springer)", re.I), # year + publisher
    re.compile(r"^©\s*\d{4}", re.I),                   # copyright
    re.compile(r"ISSN\s*[\d-]+", re.I),                # ISSN
    re.compile(r"^Authorized licensed use", re.I),     # IEEE license
    re.compile(r"^Proceedings of", re.I),               # conference proceedings
    re.compile(r"^\d+-\d+-\d+-\d+"),                    # ISBN-like
    re.compile(r"^arXiv:\d+\.\d+", re.I),             # arXiv IDs
    re.compile(r"^https?://", re.I),                    # URLs
    re.compile(r"Page\s+\d+\s+of\s+\d+", re.I),      # "Page N of N" running headers
    re.compile(r"Machine\s+Learning\s+\(\d{4}\)\s+\d+:\d+", re.I),  # journal running header
    re.compile(r"^\d+\s+Page\s+\d+\s+of\s+\d+", re.I),  # "72 Page 16 of 36 ..."
    re.compile(r"^Acta\s+Avionica", re.I),              # journal name running header
    re.compile(r"Creative\s+Commons\s+Attribution", re.I),  # CC license statement
    re.compile(r"^Article\s+is\s+licensed\s+under", re.I),  # license statement
    re.compile(r"^Received\s+\d+.*accepted\s+\d+", re.I),  # "Received 12, 2021, accepted 02, 2022"
]

# Reference patterns
REF_BRACKET_RE = re.compile(r"^\[(\d+)\]\s*(.+)")     # [1] Author...
REF_DOT_RE = re.compile(r"^(\d+)\.\s+([A-Z].{20,})")  # 1. Author...

# Author-like patterns
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
AFFILIATION_WORDS = {"university", "institute", "department", "college",
                     "laboratory", "school", "center", "centre", "faculty"}


def parse_document(raw: dict) -> Document:
    """
    Parse extracted raw data into a structured Document.

    Uses font-aware parsing if block info is available, falls back to
    text-only parsing otherwise.
    """
    blocks = raw.get("blocks", [])
    text = raw.get("text", "")
    tables = raw.get("tables", [])

    if blocks and any(b.get("size", 0) > 0 for b in blocks):
        log.info("  Using font-aware parsing (%d blocks)", len(blocks))
        doc = _parse_with_fonts(blocks, text)
    else:
        log.info("  Using text-only parsing")
        doc = _parse_text_only(text)

    # Attach tables to appropriate sections
    _attach_tables(doc, tables)

    # If no tables were extracted, try to detect them from text blocks
    if not tables:
        text_tables = _detect_tables_from_blocks(blocks)
        if text_tables:
            _attach_tables(doc, text_tables)
            _strip_raw_table_data_from_bodies(doc, text_tables)
            log.info("  Detected %d tables from text blocks", len(text_tables))

    # Clean and re-index references
    doc.references = _clean_references(doc.references)

    log.info("  Parsed: title=%d chars, %d authors, abstract=%d chars, "
             "%d sections, %d references",
             len(doc.title), len(doc.authors), len(doc.abstract),
             len(doc.sections), len(doc.references))

    return doc


# ── Font-aware parsing ───────────────────────────────────────────

def _parse_with_fonts(blocks: list, full_text: str) -> Document:
    """Parse using font size/name info from blocks."""
    # Filter metadata lines
    blocks = [b for b in blocks if not _is_metadata_line(b["text"])]

    if not blocks:
        return _parse_text_only(full_text)

    # Find the most common font size (body text size)
    sizes = [b["size"] for b in blocks if b["size"] > 0]
    if not sizes:
        return _parse_text_only(full_text)

    body_size = max(set(sizes), key=sizes.count)

    # Title: largest font, typically first few blocks
    title = _extract_title_from_blocks(blocks, body_size)

    # Authors: blocks between title and abstract
    authors = _extract_authors_from_blocks(blocks, body_size, title=title)

    # Abstract
    abstract = _extract_abstract_from_blocks(blocks)

    # Keywords
    keywords = _extract_keywords_from_blocks(blocks)

    # Sections and references
    sections, references = _extract_sections_from_blocks(blocks, body_size)

    return Document(
        title=title,
        authors=authors,
        abstract=abstract,
        keywords=keywords,
        sections=sections,
        references=references,
    )


def _extract_title_from_blocks(blocks: list, body_size: float) -> str:
    """Find the title — usually the largest text near the top."""
    candidates = []
    for i, b in enumerate(blocks[:10]):  # title is in first 10 blocks
        if b["size"] > body_size * 1.2:  # significantly larger than body
            candidates.append((i, b))

    if candidates:
        # Take the first large block(s)
        title_parts = []
        first_idx = candidates[0][0]
        for idx, b in candidates:
            if idx <= first_idx + 2:  # allow multi-line titles
                title_parts.append(b["text"])
        return " ".join(title_parts).strip()

    # Fallback: first non-metadata block
    for b in blocks[:5]:
        text = b["text"].strip()
        if len(text) > 10 and not _is_metadata_line(text):
            return text

    return ""


def _extract_authors_from_blocks(blocks: list, body_size: float,
                                  title: str = "") -> List[Author]:
    """
    Extract authors from blocks between title and abstract.

    Handles two layouts:
    1. Structured "Authors" section: explicit "Authors" heading block, then
       per-author groups of [bold name, department, institution, email].
       fitz often groups each author + affiliations into ONE multi-line block:
         "Samaira Mittal\nDepartment of CS\nXYZ Inst, India\nemail@ex.com"
       → must split on \\n and classify each line individually.
    2. Traditional inline: author names crammed between title and abstract,
       no explicit "Authors" heading, no affiliations.
    """
    authors = []
    in_author_zone = False
    current_author: Optional[Author] = None

    for b in blocks[:35]:  # extended window — structured layout needs more blocks
        text = b["text"].strip()
        lower = text.lower()

        # ── Zone triggers ────────────────────────────────────────
        # Trigger 1: explicit "Authors" / "Author" heading block
        if lower.strip() in ("authors", "author"):
            in_author_zone = True
            continue

        # Trigger 2: title-sized font — we're past the title, authors follow
        if b["size"] > body_size * 1.15:
            in_author_zone = True
            continue

        # Trigger 3: block text matches the detected title — authors follow it
        if title and text.strip() == title.strip():
            in_author_zone = True
            continue

        # Stop conditions
        if lower.startswith("abstract") or lower.startswith("keywords") \
                or lower.startswith("key words") or lower.startswith("index terms"):
            break

        if not in_author_zone:
            continue

        if _is_metadata_line(text):
            continue

        # ── Process each line within the block individually ──────
        # fitz groups nearby text into multi-line blocks.  A single block
        # may contain "AuthorName\nDepartment\nUniversity, Country\nemail".
        # We must classify EACH line, not the whole block.
        lines = text.split("\n")

        # Pre-pass: merge SURNAME / GivenName pairs on adjacent lines.
        # Pattern: line is single UPPERCASE word (surname), next line is
        # single Titlecase word (given name). E.g. "SZABÓ*" + "Peter"
        merged_lines = []
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            ln_clean = re.sub(r"[*†‡§¹²³⁴⁵⁶⁷⁸⁹⁰\d]", "", ln).strip()
            # Check: single word, uppercase, 2-20 chars
            if (ln_clean and len(ln_clean.split()) == 1
                    and ln_clean[0].isupper()
                    and 2 <= len(ln_clean) <= 20
                    and not any(w in ln_clean.lower() for w in AFFILIATION_WORDS)
                    and i + 1 < len(lines)):
                next_ln = lines[i + 1].strip()
                next_clean = re.sub(r"[*†‡§¹²³⁴⁵⁶⁷⁸⁹⁰\d]", "", next_ln).strip()
                # Next line is a single Titlecase word (given name)
                if (next_clean and len(next_clean.split()) == 1
                        and next_clean[0].isupper()
                        and 2 <= len(next_clean) <= 20
                        and not any(w in next_clean.lower() for w in AFFILIATION_WORDS)):
                    # Combine as "GivenName SURNAME"
                    merged_lines.append(f"{next_clean} {ln_clean}")
                    i += 2
                    continue
            merged_lines.append(ln)
            i += 1
        lines = merged_lines

        for line in lines:
            line = line.strip()
            if not line:
                continue
            line_lower = line.lower()

            # Email → attach to current author
            email_match = EMAIL_RE.search(line)
            if email_match:
                if current_author:
                    current_author.email = email_match.group(0)
                continue

            # Affiliation (department/university/institute/etc.)
            if any(w in line_lower for w in AFFILIATION_WORDS):
                if current_author:
                    _attach_affiliation_line(current_author, line)
                continue

            # Short location-only line: "India" or "New Delhi, India"
            if current_author and len(line) < 40 and not _is_plausible_author(line):
                if "," in line:
                    parts = [p.strip() for p in line.split(",")]
                    if not current_author.city:
                        current_author.city = parts[0]
                    if not current_author.country and len(parts) > 1:
                        current_author.country = parts[-1]
                elif not current_author.country:
                    current_author.country = line
                continue

            # Try to parse as author name(s)
            names = _split_author_names(line)
            valid = [n for n in names if _is_plausible_author(n)]
            for name in valid:
                current_author = Author(name=name.strip())
                authors.append(current_author)

    return authors


def _attach_affiliation_line(author: Author, text: str) -> None:
    """
    Attach one affiliation line to an Author, filling fields in order:
    department → organization → city/country.

    Handles trailing country: "ABC University, India" → org="ABC University", country="India"
    """
    # Strip trailing country if present (short, title-case, after last comma)
    country_extracted = ""
    if "," in text:
        last_comma = text.rfind(",")
        tail = text[last_comma + 1:].strip()
        if (1 < len(tail) < 35
                and tail[0:1].isupper()
                and not any(w in tail.lower() for w in AFFILIATION_WORDS)):
            country_extracted = tail
            text = text[:last_comma].strip()

    if country_extracted and not author.country:
        author.country = country_extracted

    # Fill department first, then organization
    if not author.department:
        author.department = text
    elif not author.organization:
        author.organization = text


def _extract_abstract_from_blocks(blocks: list) -> str:
    """Extract abstract text."""
    abstract_parts = []
    in_abstract = False

    for b in blocks:
        text = b["text"].strip()
        lower = text.lower()

        if lower.startswith("abstract"):
            in_abstract = True
            # Remove the "Abstract" prefix
            rest = re.sub(r"^abstract[\s—:.-]*", "", text, flags=re.I).strip()
            if rest:
                abstract_parts.append(rest)
            continue

        if in_abstract:
            # Stop at keywords or first section heading
            if lower.startswith("keywords") or lower.startswith("key words"):
                break
            if lower.startswith("index terms"):
                break
            if _is_heading_line(text):
                break
            abstract_parts.append(text)

    return " ".join(abstract_parts).strip()


def _extract_keywords_from_blocks(blocks: list) -> List[str]:
    """Extract keywords."""
    for b in blocks:
        text = b["text"].strip()
        lower = text.lower()

        for prefix in ["keywords", "key words", "index terms"]:
            if lower.startswith(prefix):
                rest = re.sub(
                    r"^(keywords|key\s*words|index\s*terms)[\s—:.-]*",
                    "", text, flags=re.I
                ).strip()
                if rest:
                    # Split by comma, semicolon, or middot
                    kws = re.split(r"[;,·•]", rest)
                    return [k.strip().rstrip(".") for k in kws if k.strip()]

    return []


def _extract_sections_from_blocks(blocks: list, body_size: float) -> tuple:
    """Extract sections and references."""
    sections = []
    references = []
    current_heading = ""
    current_depth = 1
    current_body = []
    current_positions = []   # (page, y) per body paragraph
    current_page = -1
    in_refs = False
    past_abstract = False

    def _save_current_section():
        """Helper: append current section to the list."""
        if current_heading and current_body:
            sections.append(Section(
                heading=current_heading,
                depth=current_depth,
                body="\n\n".join(current_body),
                start_page=current_page,
                body_positions=list(current_positions),
            ))

    for b in blocks:
        text = b["text"].strip()
        if not text:
            continue

        # Strip metadata lines from multi-line blocks (running headers, page numbers)
        clean_lines = []
        for line in text.split("\n"):
            line = line.strip()
            if line and not _is_metadata_line(line):
                clean_lines.append(line)
        if not clean_lines:
            continue
        text = "\n".join(clean_lines)

        lower = text.lower()

        # Block position: (page, y_top)
        b_page = b.get("page", -1)
        b_bbox = b.get("bbox", [0, 0, 0, 0])
        b_y = b_bbox[1] if b_bbox else 0.0

        # Skip until we're past abstract/keywords
        if not past_abstract:
            if lower.startswith("abstract"):
                past_abstract = True
                continue
            if _is_heading_line(text):
                past_abstract = True
            else:
                continue

        if _is_metadata_line(text):
            continue

        # Check for References section
        if re.match(r"^(references|bibliography)\s*$", lower):
            _save_current_section()
            in_refs = True
            current_heading = ""
            current_body = []
            current_positions = []
            continue

        # Check for "References" at end of a paragraph
        ref_match = re.search(r"\bReferences\s*$", text)
        if ref_match and not in_refs:
            before = text[:ref_match.start()].strip()
            if before and current_body is not None:
                current_body.append(before)
                current_positions.append((b_page, b_y))
            _save_current_section()
            in_refs = True
            current_heading = ""
            current_body = []
            current_positions = []
            continue

        if in_refs:
            # Split block text into individual lines — blocks can contain
            # multiple reference entries grouped by pdfplumber/fitz
            ref_lines = text.split("\n")
            for ref_line in ref_lines:
                ref_line = ref_line.strip()
                if not ref_line:
                    continue
                _process_ref_line(ref_line, references)
            continue

        # Detect headings
        heading_info = _detect_heading(text, b.get("size", 0), body_size)
        if heading_info:
            # Save previous section
            _save_current_section()
            current_heading = heading_info["text"]
            current_depth = heading_info["depth"]
            current_page = b.get("page", -1)
            current_body = []
            current_positions = []
        else:
            current_body.append(text)
            current_positions.append((b_page, b_y))

    # Save last section
    _save_current_section()

    return sections, references


def _detect_heading(text: str, font_size: float, body_size: float) -> Optional[dict]:
    """Detect if a text line is a section heading. Returns {text, depth} or None."""
    stripped = text.strip()

    # Reject page headers / running headers before heading detection
    if _is_metadata_line(stripped):
        return None
    # Also check individual lines of multi-line blocks
    first_line = stripped.split("\n")[0].strip()
    if _is_metadata_line(first_line):
        return None

    # Numbered headings: "1. Introduction", "2.1 Method"
    m = NUMBERED_HEADING_RE.match(stripped)
    if m:
        num = m.group(1).rstrip(".")
        heading_text = m.group(2).strip()
        # Reject if heading text has no alphabetic chars (just numbers/symbols)
        if not any(c.isalpha() for c in heading_text):
            return None
        # Reject if heading text is too long (likely body text)
        if len(heading_text) > 80:
            return None
        # Reject if heading text looks like a running header
        if re.search(r"Page\s+\d+\s+of\s+\d+", heading_text, re.I):
            return None
        if re.search(r"Machine\s+Learning\s+\(\d{4}\)", heading_text, re.I):
            return None
        # Determine depth from numbering: "1" = 1, "1.1" = 2, "1.1.1" = 3
        depth = num.count(".") + 1 if "." in num else 1
        return {"text": heading_text, "depth": min(depth, 3)}

    # Roman numeral headings: "I. Introduction", "II. Method"
    rom_match = re.match(r"^([IVXivx]+\.?)\s+(.+)$", stripped)
    if rom_match:
        heading_text = rom_match.group(2).strip()
        # Reject running headers
        if re.search(r"Page\s+\d+\s+of\s+\d+", heading_text, re.I):
            return None
        if heading_text.lower() in KEYWORD_HEADINGS or font_size > body_size * 1.1:
            return {"text": heading_text, "depth": 1}

    # Keyword-based headings (must be short and match known patterns)
    if stripped.lower() in KEYWORD_HEADINGS and len(stripped) < 40:
        return {"text": stripped, "depth": 1}

    # Font-size based: significantly larger than body
    if font_size > body_size * 1.15 and len(stripped) < 60:
        # Likely a heading — check it's not just a short body line
        if stripped and stripped[0].isupper() and not stripped.endswith(","):
            return {"text": stripped, "depth": 1}

    return None


# ── Text-only parsing (fallback) ─────────────────────────────────

def _parse_text_only(text: str) -> Document:
    """Parse from raw text without font information."""
    lines = text.split("\n")
    lines = [l.strip() for l in lines]
    lines = [l for l in lines if l and not _is_metadata_line(l)]

    if not lines:
        return Document()

    # Title: first substantial line
    title = ""
    start_idx = 0
    for i, line in enumerate(lines[:5]):
        if len(line) > 10:
            title = line
            start_idx = i + 1
            break

    # Abstract
    abstract = ""
    abstract_end = start_idx
    for i in range(start_idx, min(len(lines), start_idx + 30)):
        lower = lines[i].lower()
        if lower.startswith("abstract"):
            rest = re.sub(r"^abstract[\s—:.-]*", "", lines[i], flags=re.I).strip()
            abs_parts = [rest] if rest else []
            for j in range(i + 1, min(len(lines), i + 20)):
                if _is_heading_line(lines[j]):
                    abstract_end = j
                    break
                if lines[j].lower().startswith("keywords"):
                    abstract_end = j
                    break
                abs_parts.append(lines[j])
                abstract_end = j + 1
            abstract = " ".join(abs_parts).strip()
            break

    # Keywords
    keywords = []
    kw_end = abstract_end
    for i in range(abstract_end, min(len(lines), abstract_end + 5)):
        lower = lines[i].lower()
        for prefix in ["keywords", "key words", "index terms"]:
            if lower.startswith(prefix):
                rest = re.sub(
                    r"^(keywords|key\s*words|index\s*terms)[\s—:.-]*",
                    "", lines[i], flags=re.I
                ).strip()
                if rest:
                    kws = re.split(r"[;,·•]", rest)
                    keywords = [k.strip().rstrip(".") for k in kws if k.strip()]
                kw_end = i + 1
                break

    # Sections and references
    sections = []
    references = []
    current_heading = ""
    current_depth = 1
    current_body = []
    in_refs = False

    for i in range(kw_end, len(lines)):
        line = lines[i]
        lower = line.lower()

        if re.match(r"^(references|bibliography)\s*$", lower):
            if current_heading and current_body:
                sections.append(Section(
                    heading=current_heading,
                    depth=current_depth,
                    body="\n\n".join(current_body),
                ))
            in_refs = True
            current_heading = ""
            current_body = []
            continue

        if in_refs:
            ref = _parse_reference_line(line, len(references) + 1)
            if ref:
                references.append(ref)
            continue

        heading_info = _detect_heading(line, 0, 0)
        if heading_info:
            if current_heading and current_body:
                sections.append(Section(
                    heading=current_heading,
                    depth=current_depth,
                    body="\n\n".join(current_body),
                ))
            current_heading = heading_info["text"]
            current_depth = heading_info["depth"]
            current_body = []
        else:
            current_body.append(line)

    if current_heading and current_body:
        sections.append(Section(
            heading=current_heading,
            depth=current_depth,
            body="\n\n".join(current_body),
        ))

    return Document(
        title=title,
        abstract=abstract,
        keywords=keywords,
        sections=sections,
        references=references,
    )


# ── Helper functions ─────────────────────────────────────────────

def _is_metadata_line(text: str) -> bool:
    """Check if a line is page header/footer metadata."""
    text = text.strip()
    if not text:
        return True
    for pat in METADATA_PATTERNS:
        if pat.search(text):
            return True
    return False


def _is_heading_line(text: str) -> bool:
    """Quick check if text looks like a section heading."""
    text = text.strip()
    if NUMBERED_HEADING_RE.match(text):
        return True
    if text.lower() in KEYWORD_HEADINGS:
        return True
    rom = re.match(r"^[IVX]+\.?\s+\w+", text)
    if rom and len(text) < 60:
        return True
    return False


def _split_author_names(text: str) -> List[str]:
    """Split a line into individual author names."""
    # Remove superscript digits
    text = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰\d*†‡§]", "", text)
    # Split by common separators
    if "," in text:
        parts = text.split(",")
    elif " and " in text.lower():
        parts = re.split(r"\s+and\s+", text, flags=re.I)
    elif "·" in text or "•" in text:
        parts = re.split(r"[·•]", text)
    else:
        parts = [text]
    return [p.strip() for p in parts if p.strip()]


def _is_plausible_author(name: str) -> bool:
    """Check if a string looks like an author name."""
    name = name.strip()
    if len(name) < 3 or len(name) > 60:
        return False
    if name[0].islower():
        return False
    # Reject if it's all caps and long (likely an acronym or heading)
    if name.isupper() and len(name) > 5:
        return False
    # Reject affiliation words
    lower = name.lower()
    if any(w in lower for w in AFFILIATION_WORDS):
        return False
    if any(w in lower for w in ["the ", "ieee", "acm"]):
        return False
    # Must have at least 2 words
    if len(name.split()) < 2:
        return False
    # Reject hyphens (likely compound terms, not names)
    if "-" in name and not " " in name.split("-")[0]:
        pass  # Allow hyphenated surnames like "Mary-Jane"
    return True


def _process_ref_line(text: str, references: list):
    """Process a single line from the references section."""
    # Skip metadata/junk lines
    if _is_junk_reference(text):
        return

    # ── Priority 1: Numbered references [1] or 1. ──
    has_bracket = REF_BRACKET_RE.match(text)
    has_dot = REF_DOT_RE.match(text)
    if has_bracket or has_dot:
        ref = _parse_reference_line(text, len(references) + 1)
        if ref:
            references.append(ref)
        return

    # ── Priority 2: Author-year references ──
    # Match patterns like "Surname, F.", "Surname, F. M.", "Surname, F.,"
    author_start = re.match(
        r"^[A-Z][a-zÀ-ÿ]+(?:[-'][A-Z][a-zÀ-ÿ]+)?,\s+"
        r"[A-Z]\.(?:\s*[A-Z]\.)*", text)
    if author_start:
        has_year = re.search(r"\(\d{4}[a-z]?\)", text)
        if has_year:
            references.append(Reference(
                text=text,
                index=len(references) + 1,
                author_year=True,
            ))
            return

    # Also match "Surname, F., Surname, B.," patterns (multi-author, year may be later)
    # where the line starts with what looks like a new citation
    multi_author = re.match(
        r"^[A-Z][a-zÀ-ÿ]+(?:[-'][A-Z][a-zÀ-ÿ]+)?,\s+"
        r"(?:[A-Z]\.(?:\s*[A-Z]\.)*,?\s*(?:&\s*)?){1,}", text)
    if multi_author and len(text) > 40:
        # Check if year appears later in this line
        has_year = re.search(r"\(\d{4}[a-z]?\)", text)
        if has_year:
            references.append(Reference(
                text=text,
                index=len(references) + 1,
                author_year=True,
            ))
            return

    # ── Priority 3: Continuation of previous reference ──
    if references and text and len(text) > 5:
        # Lines that start lowercase are always continuations
        if text[0].islower():
            references[-1].text += " " + text
            return
        # Specific continuation indicators
        if (text.startswith("pp.") or text.startswith("In:") or
                text.startswith("In ") or text.startswith("arXiv") or
                text.startswith("&") or text.startswith("and ")):
            references[-1].text += " " + text
            return
        # Short uppercase fragments that don't look like a new reference
        if text[0].isupper() and len(text) < 80:
            # If no author-initial pattern, it's continuation (journal/title text)
            if not re.match(r"^[A-Z][a-z]+,\s+[A-Z]\.", text):
                references[-1].text += " " + text
                return

    # ── Priority 4: Unmatched long line — new unnumbered ref ──
    if text and len(text) > 40 and references:
        # Check if it could be a new reference (starts with author-like pattern)
        if re.match(r"^[A-Z]", text):
            references.append(Reference(
                text=text,
                index=len(references) + 1,
            ))
        else:
            references[-1].text += " " + text
    elif text and len(text) > 40:
        references.append(Reference(
            text=text,
            index=len(references) + 1,
        ))


def _is_junk_reference(text: str) -> bool:
    """Check if a reference line is publisher boilerplate, not a real citation."""
    text_stripped = text.strip()
    lower = text_stripped.lower()

    # Journal header lines like "Machine Learning (2026) 115:72 Page 33 of 36"
    if re.match(r"^[a-z\s]+ +\(\d{4}\)\s*\d+:\d+", lower):
        return True
    # Email-only lines
    if re.match(r"^[a-z0-9_.+-]+@[a-z0-9-]+\.[a-z.]+$", lower):
        return True
    # Page numbers like "1 3" or just digits
    if re.match(r"^\d+(\s+\d+)?$", text_stripped):
        return True
    # Publisher notes
    # License / CC / received-accepted statements
    if re.search(r"creative\s+commons", lower):
        return True
    if re.search(r"article\s+is\s+licensed\s+under", lower):
        return True
    if re.search(r"received\s+\d+.*accepted\s+\d+", lower):
        return True
    junk_starts = [
        "publisher's note", "springer nature", "exclusive rights",
        "manu script version", "author self-archiving",
        "page ", "machine learning",
    ]
    for js in junk_starts:
        if lower.startswith(js):
            return True
    # Lines that are just journal/volume info
    if re.match(r"^(volume|vol\.?|issue|no\.?)\s", lower):
        return True
    # Page header patterns: "72 Page 34 of 36 Machine Learning (2026)..."
    if re.search(r"page\s+\d+\s+of\s+\d+", lower):
        return True
    # Journal footer with page number: "72 Page..."
    if re.match(r"^\d+\s+page\s+\d+", lower):
        return True
    # Author blocks with interpuncts (appears at end of papers)
    if "·" in text_stripped and re.search(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]", text_stripped):
        return True
    # Affiliation-only lines (numbered affiliations at end)
    if re.match(r"^\d+\s+(University|Institute|Department|Lab|School|Vector|Noah)", text_stripped):
        return True
    # Very short lines that are just noise
    if len(text_stripped) < 15 and not re.match(r"\[\d+\]", text_stripped):
        return True
    return False


def _parse_reference_line(text: str, default_idx: int) -> Optional[Reference]:
    """Parse a reference line into a Reference object."""
    text = text.strip()
    if len(text) < 15:
        return None

    # Filter out junk
    if _is_junk_reference(text):
        return None

    # [1] Author, Title...
    m = REF_BRACKET_RE.match(text)
    if m:
        ref_text = m.group(2).strip()
        if _is_junk_reference(ref_text):
            return None
        return Reference(
            text=ref_text,
            index=int(m.group(1)),
        )

    # 1. Author, Title...
    m = REF_DOT_RE.match(text)
    if m:
        ref_text = m.group(2).strip()
        if _is_junk_reference(ref_text):
            return None
        return Reference(
            text=ref_text,
            index=int(m.group(1)),
        )

    # Continuation of previous reference or unlabeled reference
    if text[0].isupper() and len(text) > 30:
        return Reference(text=text, index=default_idx)

    return None


def _reconstruct_numbers(tokens: list) -> list:
    """
    Reconstruct decimal numbers from space-fragmented tokens.

    PDF extraction often splits "1.26" into ["1", ".", "26"] or ["1", "26"]
    and annotation symbols into separate tokens like ["⇑"].

    This function merges adjacent tokens that form a single number+annotation.
    """
    if not tokens:
        return []

    # Annotation symbols that may appear after numbers
    ANNOT_CHARS = set("⇑⇓↑↓†‡§*⁺⁻")

    result = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Skip standalone annotation symbols — they'll attach to previous number
        if all(c in ANNOT_CHARS for c in tok):
            if result:
                result[-1] += tok
            i += 1
            continue

        # Check if this starts a fragmented number: digit(s) [.] digit(s) [annotation]
        if tok.replace("-", "").replace("+", "").isdigit():
            num = tok
            j = i + 1

            # Check for decimal point
            if j < len(tokens) and tokens[j] == ".":
                if j + 1 < len(tokens) and tokens[j + 1].isdigit():
                    num = f"{tok}.{tokens[j + 1]}"
                    j += 2
                else:
                    num += "."
                    j += 1
            # Also handle "1 26" → "1.26" (two adjacent digit groups with no dot)
            elif (j < len(tokens) and tokens[j].isdigit()
                    and len(tok) <= 2 and len(tokens[j]) <= 3):
                # Heuristic: small numbers followed by more digits = decimal
                num = f"{tok}.{tokens[j]}"
                j += 1

            # Attach trailing annotations
            while j < len(tokens) and all(c in ANNOT_CHARS for c in tokens[j]):
                num += tokens[j]
                j += 1

            result.append(num)
            i = j
        else:
            result.append(tok)
            i += 1

    return result


def _detect_tables_from_blocks(blocks: list) -> list:
    """
    Detect tables from text blocks when pdfplumber fails to find gridded tables.

    Handles two block structures:
    1. All-in-one: caption + headers + data in a single large block (pdfplumber)
    2. Multi-block: caption in one block, headers/data in subsequent blocks (PyMuPDF)

    Handles space-fragmented numbers by reconstructing decimals from adjacent tokens.
    Supports multi-level headers (e.g. dataset names + sub-columns per dataset).

    Returns list of table dicts: [{headers, rows, caption, label}]
    """
    from collections import Counter
    tables = []
    table_caption_re = re.compile(r"^Table\s+(\d+)\b[.\s]*(.*)", re.IGNORECASE)

    # Known dataset header patterns (common in ML papers)
    DATASET_HEADER_RE = re.compile(
        r"^(Skillcraft|SML|Parkinsons|Bike|CCPP|CIFAR|MNIST|ImageNet|"
        r"SVHN|Fashion|STL|Caltech|Reuters|AG.News|DBPedia|Amazon|Yelp)"
    )

    def _is_header_line(line: str) -> bool:
        """Check if a line looks like a table header row (not caption/sentence text)."""
        words = line.split()
        if len(words) < 3 or len(words) > 12:
            return False

        # Reject lines with sentence punctuation (these are captions, not headers)
        SENTENCE_PUNCTS = {".", ",", ";", ":", "!", "?"}
        punct_words = sum(1 for w in words if any(c in SENTENCE_PUNCTS for c in w))
        if punct_words >= 2:
            return False

        # Reject lines with too many common English words (sentence text)
        COMMON_WORDS = {"the", "a", "an", "and", "or", "is", "are", "was", "were",
                        "to", "of", "in", "for", "with", "on", "at", "by", "from",
                        "that", "this", "it", "not", "we", "our", "be", "as", "has",
                        "have", "had", "but", "if", "than", "its", "all", "can",
                        "will", "which", "their", "more", "also", "only", "denote",
                        "reported", "setting", "results", "worse", "better",
                        "significantly", "respectively", "similarly"}
        common_count = sum(1 for w in words if w.lower().rstrip(".,;:") in COMMON_WORDS)
        if common_count >= 2:
            return False

        alpha_words = [w for w in words if w.isalpha() and 1 < len(w) < 20]
        has_digits = any(c.isdigit() for c in line)

        # Dataset header pattern — strong signal
        if sum(1 for w in words if DATASET_HEADER_RE.match(w)) >= 2:
            return True

        # Classic header: 3+ short alphabetic words, no digits, no common words
        if (len(alpha_words) >= 3 and not has_digits
                and all(len(w) < 20 for w in words)):
            return True

        return False

    def _is_subheader_line(line: str) -> bool:
        """Check if line is a sub-header (e.g. '#Clients 10 100 10 100...')."""
        tokens = line.split()
        if not tokens:
            return False
        first = tokens[0].lower()
        if first.startswith(("#client", "train", "test", "corr", "size")):
            return True
        # All-number sub-header like "10 100 10 100 10 100 10 100 10 100"
        num_tokens = [t for t in tokens if t.isdigit()]
        if len(num_tokens) >= 6 and len(num_tokens) == len(tokens):
            return True
        return False

    def _is_metadata_table_line(line: str) -> bool:
        """Check if line is metadata between header and data."""
        tokens = line.split()
        if not tokens:
            return True
        first = tokens[0].lower()
        return (
            first.startswith(("train", "test", "corr", "size"))
            or "/" in first
            or first == "−"
            or (len(line) < 5 and all(c in "−-– " for c in line))
        )

    def _parse_data_line(line: str):
        """Parse a single data line into (method_name, values) or None."""
        tokens = line.split()
        reconstructed = _reconstruct_numbers(tokens)

        method_parts = []
        value_parts = []
        for tok in reconstructed:
            if re.match(r"^[\d\.\-\+±e]+[⇑⇓↑↓†‡§\*]*$", tok):
                value_parts.append(tok)
            elif value_parts:
                value_parts.append(tok)
            else:
                method_parts.append(tok)

        method_name = " ".join(method_parts)
        return method_name, value_parts

    def _extract_table_from_lines(lines: list, table_num: str, caption_text: str):
        """
        Extract a table from a list of text lines (may come from one or more blocks).
        Returns (headers, sub_headers, rows, caption) or None.
        """
        header_line = ""
        sub_header_labels = []
        data_lines = []
        phase = "caption"  # caption -> header -> subheader -> data

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Skip annotation-only lines
            if len(line) < 5 and not any(c.isalnum() for c in line if c.isascii()):
                continue
            # Skip page headers
            if re.match(r"^\d+\s+Page\s+\d+", line, re.I):
                continue
            if _is_metadata_line(line):
                continue
            if re.match(r"^\d{1,3}\s+\d{1,3}$", line):
                continue

            if phase == "caption":
                # Look for header line
                if _is_header_line(line):
                    header_line = line
                    phase = "subheader"
                elif _is_subheader_line(line):
                    # Sub-header before main header — caption line with #Clients first
                    # The header might be on the next line
                    sub_header_labels = _extract_sub_values(line)
                    phase = "subheader"
                # else: still in caption/annotation text — skip
                continue

            elif phase == "subheader":
                # Check if this is actually the main header (comes after #Clients label)
                if _is_header_line(line):
                    header_line = line
                    continue
                if _is_subheader_line(line):
                    vals = _extract_sub_values(line)
                    if vals:
                        sub_header_labels = vals
                    continue
                elif _is_metadata_table_line(line):
                    continue
                # Check if this is a pure number line (sub-header values or metadata)
                tokens = line.split()
                all_numeric = all(
                    re.match(r"^[\d\.\-\+±e/]+$", t) or t in ("−", "-", "–")
                    for t in tokens
                ) if tokens else False
                if all_numeric and len(tokens) >= 3:
                    # If values are small integers repeated, likely sub-header
                    int_vals = [t for t in tokens if t.isdigit()]
                    if len(int_vals) >= 4:
                        sub_header_labels.extend(int_vals)
                    # Otherwise it's a metadata row (corr-coef values etc.)
                    continue
                # Annotation-only lines (⇑ ⇓ etc.)
                if all(c in "⇑⇓↑↓†‡§*− \t" for c in line):
                    continue
                # Must be start of data
                phase = "data"
                # Fall through to data processing

            if phase == "data":
                # Stop conditions
                if table_caption_re.match(line):
                    break
                if re.match(r"^(Fig\.|Figure)\s+\d+", line, re.I):
                    break
                if line.lower() in ("references", "bibliography"):
                    break
                if line.lower() == "ours":
                    continue
                # Skip lines that are just dots (fragmented decimal points)
                if all(c in ". \t" for c in line):
                    continue
                # Skip annotation-only lines (⇑ ⇓ etc.)
                if all(c in "⇑⇓↑↓†‡§*− \t" for c in line):
                    continue

                method_name, value_parts = _parse_data_line(line)

                # Stop if body text
                clean_nums = sum(1 for v in value_parts
                                 if re.match(r"^[\d\.\-\+±e]+[⇑⇓↑↓†‡§\*]*$", v))
                if method_name and len(method_name) > 40 and clean_nums < 3:
                    break
                tokens = line.split()
                if len(tokens) > 8 and clean_nums == 0 and method_name:
                    break

                if method_name or value_parts:
                    data_lines.append((method_name, value_parts))

        return header_line, sub_header_labels, data_lines

    def _extract_sub_values(line: str) -> list:
        """Extract numeric sub-header values from a line like '#Clients 10 100 10 100'."""
        tokens = line.split()
        vals = []
        for t in tokens:
            if t.isdigit() or re.match(r"^\d+[eE]\d+$", t):
                vals.append(t)
        return vals

    # ── Main loop: find Table N blocks ──
    i = 0
    while i < len(blocks):
        text = blocks[i]["text"].strip()

        # Look for "Table N" caption
        cap_match = table_caption_re.match(text)
        if not cap_match:
            i += 1
            continue

        table_num = cap_match.group(1)
        log.debug("  Table %s caption found at block %d", table_num, i)

        # Collect ALL lines from this block and subsequent blocks that might be table data
        all_lines = []
        caption_first_line = ""

        # Parse lines from the caption block itself
        block_lines = text.split("\n")
        caption_first_line = block_lines[0].strip() if block_lines else text
        # Add all lines from this block (including caption — the parser will skip it)
        all_lines.extend(block_lines)

        # Also collect lines from subsequent blocks (table data might span blocks)
        j = i + 1
        while j < len(blocks) and j < i + 10:
            btext = blocks[j]["text"].strip()
            if not btext:
                j += 1
                continue
            # Stop if we hit another table caption
            if table_caption_re.match(btext):
                break
            # Stop if we hit a section heading
            if re.match(r"^\d+\.?\d*\s+[A-Z]", btext) and len(btext) < 80:
                hmatch = NUMBERED_HEADING_RE.match(btext.split("\n")[0].strip())
                if hmatch:
                    break
            # Stop if block looks like pure body text (long, no numbers)
            if len(btext) > 200 and len(re.findall(r"\d+\.\d+", btext)) < 3:
                break
            all_lines.extend(btext.split("\n"))
            j += 1

        # Parse the collected lines
        header_line, sub_header_labels, data_lines = _extract_table_from_lines(
            all_lines, table_num, caption_first_line
        )

        if not header_line and not data_lines:
            log.debug("  Table %s: no header or data found", table_num)
            i += 1
            continue

        # Build headers from header line
        headers = [w for w in header_line.split() if len(w) > 0] if header_line else []
        log.debug("  Table %s primary headers: %s", table_num, headers)

        # Determine actual data column count from data rows
        # First: accumulate values per method (data may span multiple lines)
        accumulated_rows = []
        acc_method = ""
        acc_values = []
        for method_name, values in data_lines:
            clean = [v for v in values
                     if re.match(r"^[\d\.\-\+±e]+[⇑⇓↑↓†‡§\*]*$", v)]
            if method_name:
                if acc_method and acc_values:
                    accumulated_rows.append((acc_method, acc_values))
                acc_method = method_name
                acc_values = list(clean)
            else:
                acc_values.extend(clean)
        if acc_method and acc_values:
            accumulated_rows.append((acc_method, acc_values))

        if not accumulated_rows:
            log.debug("  Table %s: no data values found", table_num)
            i += 1
            continue

        actual_value_counts = [len(vals) for _, vals in accumulated_rows[:8] if vals]
        val_count_freq = Counter(actual_value_counts)
        actual_data_cols = val_count_freq.most_common(1)[0][0]

        # If sub-headers give a clear column count, prefer that
        if sub_header_labels and len(sub_header_labels) >= 4:
            sub_cols = len(sub_header_labels)
            # Use sub-header count if it matches any accumulated row count
            if sub_cols in actual_value_counts or sub_cols >= actual_data_cols:
                actual_data_cols = sub_cols

        log.debug("  Table %s actual data cols: %d (from %s, sub=%d)", table_num,
                  actual_data_cols, actual_value_counts,
                  len(sub_header_labels))

        # Build merged headers with sub-headers
        if sub_header_labels and len(sub_header_labels) >= actual_data_cols and len(headers) > 0:
            sub_vals = sub_header_labels[:actual_data_cols]
            cols_per_header = actual_data_cols // len(headers) if headers else 0
            if cols_per_header > 1 and actual_data_cols == len(headers) * cols_per_header:
                merged_headers = []
                for hi, h in enumerate(headers):
                    for si in range(cols_per_header):
                        idx = hi * cols_per_header + si
                        if idx < len(sub_vals):
                            merged_headers.append(f"{h}({sub_vals[idx]})")
                        else:
                            merged_headers.append(h)
                headers = merged_headers
        elif actual_data_cols > len(headers) and len(headers) > 0 and actual_data_cols <= len(headers) * 3:
            cols_per_header = actual_data_cols // len(headers)
            if cols_per_header > 1 and actual_data_cols == len(headers) * cols_per_header:
                merged_headers = []
                for h in headers:
                    for si in range(1, cols_per_header + 1):
                        merged_headers.append(f"{h}_{si}")
                headers = merged_headers

        # If we still don't have enough headers, generate generic ones
        if len(headers) < actual_data_cols:
            if headers:
                # Pad with numbered headers
                while len(headers) < actual_data_cols:
                    headers.append(f"Col{len(headers)+1}")
            else:
                headers = [f"Col{ci+1}" for ci in range(actual_data_cols)]

        num_data_cols = len(headers)
        if num_data_cols < 2:
            i += 1
            continue

        log.debug("  Table %s final headers (%d cols): %s", table_num, num_data_cols, headers[:8])

        # Build rows from data_lines, accumulating values per method
        rows = []
        current_method = ""
        current_values = []

        for method_name, value_parts in data_lines:
            if method_name and current_method and current_values:
                row = _build_table_row(current_method, current_values, num_data_cols)
                if row:
                    rows.append(row)
                current_method = method_name
                current_values = value_parts
            elif method_name and not current_method:
                current_method = method_name
                current_values = value_parts
            elif value_parts:
                current_values.extend(value_parts)

        # Save last row
        if current_method and current_values:
            row = _build_table_row(current_method, current_values, num_data_cols)
            if row:
                rows.append(row)

        if rows:
            # Extract clean caption
            cap_clean = re.sub(r"^Table\s+\d+\.?\s*", "", caption_first_line, flags=re.I).strip()
            # If caption is too short or just symbols, scan subsequent lines
            if len(cap_clean) < 10 or not any(c.isalpha() and c.isascii() for c in cap_clean):
                for bl in block_lines[1:]:
                    bl = bl.strip()
                    if bl and len(bl) > 15 and any(c.isalpha() and c.isascii() for c in bl):
                        cap_clean = bl
                        break
            if len(cap_clean) > 120:
                sent_end = re.search(r"[.!?]\s", cap_clean[:120])
                if sent_end:
                    cap_clean = cap_clean[:sent_end.end()].strip()
            # Clean up annotation symbols and remnants from captions
            cap_clean = re.sub(r"\s*[⇑⇓↑↓]+\s*", " ", cap_clean)
            cap_clean = re.sub(r"\s*,\s*,\s*,?\s*and\s+$", "", cap_clean)
            cap_clean = re.sub(r"\s+and\s+denote\s+(sig|significantly).*$", "", cap_clean, flags=re.I)
            cap_clean = re.sub(r"\s{2,}", " ", cap_clean).strip()

            final_headers = ["Method"] + headers

            # Quality checks
            avg_first_cell_len = sum(len(r[0]) for r in rows) / len(rows)
            has_numeric_data = any(
                any(re.search(r"\d+[\.\d]*", cell) for cell in r[1:])
                for r in rows
            )

            # Check headers: allow composite headers like "Skillcraft(10)"
            bad_header_count = sum(
                1 for h in headers
                if (h.endswith((".",":","," ,";"))
                    or h.lower() in ("the","a","an","we","and","or","is","are","to","of",
                                     "in","for","with","on","at","by","from","that","this",
                                     "them","then","also","only"))
            )
            headers_look_ok = bad_header_count <= 1

            clean_data_rows = 0
            for r in rows[:5]:
                clean_vals = sum(
                    1 for cell in r[1:]
                    if re.match(r"^[\d\.\-\+±e]+[⇑⇓↑↓†‡§\*]*$", cell.strip())
                )
                if clean_vals >= 2:
                    clean_data_rows += 1
            has_clean_data = clean_data_rows >= min(2, len(rows[:5]))

            if (avg_first_cell_len < 80 and has_numeric_data
                    and headers_look_ok and has_clean_data):
                tables.append({
                    "headers": final_headers,
                    "rows": rows,
                    "caption": cap_clean,
                    "label": f"tab:{table_num}",
                })
                log.info("  Table %s detected: %d cols x %d rows",
                         table_num, len(final_headers), len(rows))
            else:
                log.debug("  Skipping misdetected table %s "
                          "(avg_cell=%d, numeric=%s, headers_ok=%s, clean_data=%s)",
                          table_num, avg_first_cell_len, has_numeric_data,
                          headers_look_ok, has_clean_data)

            i = j
        else:
            i += 1

    return tables


def _build_table_row(method: str, values: list, expected_data_cols: int) -> list:
    """
    Build a table row with [method_name, val1, val2, ...].

    Distributes values across expected columns. If we have fewer values,
    pad with empty. If more, try to merge or truncate.
    """
    if not method:
        return []

    # Clean up values — remove pure annotation tokens
    clean_values = []
    for v in values:
        # Skip pure annotation/symbol tokens
        if all(c in "⇑⇓↑↓†‡§*. " for c in v):
            continue
        clean_values.append(v)

    row = [method]

    # Distribute values to match expected column count
    if len(clean_values) <= expected_data_cols:
        row.extend(clean_values)
    else:
        # More values than columns — take only expected count
        row.extend(clean_values[:expected_data_cols])

    # Pad to expected total columns (method + data)
    while len(row) < expected_data_cols + 1:
        row.append("")

    return row


def _clean_references(refs: List[Reference]) -> List[Reference]:
    """
    Post-process references:
    1. Remove junk entries (publisher boilerplate, emails, journal-only lines)
    2. Remove duplicate entries
    3. Re-index sequentially starting from 1
    4. Clean up reference text formatting
    """
    cleaned = []
    seen_texts = set()

    for ref in refs:
        text = ref.text.strip()

        # Skip empty
        if not text or len(text) < 15:
            continue

        # Skip junk
        if _is_junk_reference(text):
            continue

        # Skip email-only refs
        if re.match(r"^[A-Za-z0-9_.+-]+\s+[a-z0-9_.+-]+@", text):
            continue

        # Skip refs that are just author names + emails (no title)
        if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+\s+[a-z0-9_.+-]+@", text):
            continue

        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # Deduplicate (by first 50 chars to catch near-duplicates)
        key = text[:50].lower()
        if key in seen_texts:
            continue
        seen_texts.add(key)

        cleaned.append(Reference(text=text, index=len(cleaned) + 1,
                                 author_year=ref.author_year))

    return cleaned


def _attach_tables(doc: Document, tables: list):
    """
    Attach extracted tables to the nearest appropriate section.

    Strategy:
    - Tables with captions mentioning section keywords → attach to matching section
    - Tables from specific pages → attach to section on that page
    - Fallback: distribute to sections with body text, preferring "results"/"experiment"
    """
    if not tables or not doc.sections:
        return

    table_objects = []
    for t in tables:
        tbl = Table(
            headers=t.get("headers", []),
            rows=t.get("rows", []),
            caption=t.get("caption", ""),
            label=t.get("label", ""),
        )
        # Skip empty tables (no real data)
        if not tbl.headers and not tbl.rows:
            continue
        # Skip tables with only 1 column and 1 row (likely parsing artifacts)
        num_cols = len(tbl.headers) if tbl.headers else (
            len(tbl.rows[0]) if tbl.rows else 0)
        if num_cols == 0:
            continue
        table_objects.append(tbl)

    if not table_objects:
        return

    # Find the best section to attach tables to
    body_sections = [s for s in doc.sections if s.body.strip()]
    if not body_sections:
        body_sections = doc.sections

    # Prefer results/experiment sections for data tables
    results_sections = [
        s for s in body_sections
        if any(kw in s.heading.lower() for kw in
               ("result", "experiment", "evaluation", "performance", "comparison"))
    ]

    for i, table in enumerate(table_objects):
        if results_sections:
            # Distribute among results-like sections
            idx = i % len(results_sections)
            results_sections[idx].tables.append(table)
        else:
            # Fallback: distribute among body sections, later sections first
            # (tables tend to appear in the latter half of papers)
            target_start = max(0, len(body_sections) // 2)
            target_sections = body_sections[target_start:] or body_sections
            idx = i % len(target_sections)
            target_sections[idx].tables.append(table)


def _strip_raw_table_data_from_bodies(doc: Document, text_tables: list):
    """
    Remove raw table text from section bodies after text-based table detection.

    When tables are detected from text blocks, the same text (captions, headers,
    data rows, annotation symbols) still appears in section bodies. This function
    strips that raw text to prevent duplicate rendering.
    """
    if not text_tables:
        return

    # Collect all unique dataset header words from all tables
    all_dataset_headers = set()
    for table_info in text_tables:
        headers = table_info.get("headers", [])
        for h in headers[1:]:  # Skip "Method"
            # Extract base name from composite headers like "Skillcraft(10)"
            base = re.sub(r"\([^)]*\)", "", h).strip()
            base = re.sub(r"_\d+$", "", base).strip()
            if base and base.isalpha() and len(base) > 2:
                all_dataset_headers.add(base)

    removal_patterns = []

    for table_info in text_tables:
        headers = table_info.get("headers", [])
        rows = table_info.get("rows", [])

        # Match "Table N ..." caption line
        label = table_info.get("label", "")
        if label:
            table_num = label.replace("tab:", "")
            removal_patterns.append(
                re.compile(r"Table\s+" + re.escape(table_num) + r"\b[^.]*$",
                           re.I | re.MULTILINE))

        # Match data row patterns (method name followed by decimal numbers)
        for row in rows:
            if row and row[0]:
                method = re.escape(row[0])
                removal_patterns.append(
                    re.compile(method + r"\s+[\d\.\s⇑⇓↑↓†‡§\*±]+"))

    # Build dataset header line pattern (e.g. "Skillcraft SML Parkinsons Bike CCPP")
    if len(all_dataset_headers) >= 3:
        # Match any line that is ONLY dataset names (no other context)
        dataset_words = "|".join(re.escape(h) for h in all_dataset_headers)
        removal_patterns.append(
            re.compile(r"^(" + dataset_words + r")(\s+(" + dataset_words + r"))+\s*$",
                       re.MULTILINE))

    # Global patterns for table remnants
    removal_patterns.extend([
        # Annotation symbols on their own lines
        re.compile(r"^[⇑⇓↑↓\s]+$", re.MULTILINE),
        re.compile(r"^similarly\s*$", re.MULTILINE),
        re.compile(r"^similarly\s+to\s+Table\s+\d+\s*$", re.MULTILINE | re.I),
        # Metadata rows
        re.compile(r"Train/test\s+size.*$", re.MULTILINE),
        re.compile(r"corr-coef.*$", re.MULTILINE),
        re.compile(r"^#[Cc]lients?\s*$", re.MULTILINE),
        re.compile(r"^#[Cc]lients?\s+\d+.*$", re.MULTILINE),
        # Method names that are known table rows
        re.compile(r"^Central\s+GP\s+[\d\.\s]+$", re.MULTILINE),
        re.compile(r"^(Local\+local|Local\+global|Avg\+local|kd\+local)\b.*$",
                    re.MULTILINE),
        re.compile(r"^ours\s*$", re.MULTILINE | re.I),
        # Lines that are mostly numbers with annotation symbols
        re.compile(r"^[\d\.\s⇑⇓↑↓†‡§\*±,/]+$", re.MULTILINE),
        # Fragmented p-value lines from table annotations
        re.compile(r"^p\s*<\s*\.\s*p\s*<\s*\.?\s*$", re.MULTILINE),
        # Lines with just "− −" or similar
        re.compile(r"^[−\-–\s]+$", re.MULTILINE),
        # "nificantly worse results..." (split caption text)
        re.compile(r"^nificantly\s+worse\s+results.*$", re.MULTILINE),
        # "results similarly" leftover
        re.compile(r"^results\s+similarly\s*$", re.MULTILINE),
        # "similarly to Table N" leftover
        re.compile(r"^similarly\s+to\s*$", re.MULTILINE),
        # "ECE reported..." leftover caption
        re.compile(r"^ECE\s+reported\s+for\s+the\s+.*setting\..*$", re.MULTILINE),
        # Annotation descriptions from table captions
        re.compile(r"^.*denote\s+significantly\s+(worse|better)\s+results.*$",
                    re.MULTILINE),
        # "p < 0.01" and "p < 0.05" annotation lines
        re.compile(r"^\s*p\s*<\s*0?\s*\.?\s*0[15]\s*$", re.MULTILINE),
        # Size data rows like "2670/334 3309/414 ..."
        re.compile(r"^\d+/\d+(\s+\d+/\d+)+\s*$", re.MULTILINE),
    ])

    # Apply removal to all section bodies
    for section in doc.sections:
        body = section.body
        for pat in removal_patterns:
            body = pat.sub("", body)
        # Clean up multiple blank lines
        body = re.sub(r"\n{3,}", "\n\n", body)
        section.body = body.strip()
