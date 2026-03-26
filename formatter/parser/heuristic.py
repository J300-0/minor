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
    current_page = -1
    in_refs = False
    past_abstract = False

    for b in blocks:
        text = b["text"].strip()
        if not text:
            continue

        lower = text.lower()

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
            # Save current section
            if current_heading and current_body:
                sections.append(Section(
                    heading=current_heading,
                    depth=current_depth,
                    body="\n\n".join(current_body),
                    start_page=current_page,
                ))
            in_refs = True
            current_heading = ""
            current_body = []
            continue

        # Check for "References" at end of a paragraph
        ref_match = re.search(r"\bReferences\s*$", text)
        if ref_match and not in_refs:
            before = text[:ref_match.start()].strip()
            if before and current_body is not None:
                current_body.append(before)
            if current_heading and current_body:
                sections.append(Section(
                    heading=current_heading,
                    depth=current_depth,
                    body="\n\n".join(current_body),
                    start_page=current_page,
                ))
            in_refs = True
            current_heading = ""
            current_body = []
            continue

        if in_refs:
            # Parse reference entries
            ref = _parse_reference_line(text, len(references) + 1)
            if ref:
                references.append(ref)
            continue

        # Detect headings
        heading_info = _detect_heading(text, b.get("size", 0), body_size)
        if heading_info:
            # Save previous section
            if current_heading and current_body:
                sections.append(Section(
                    heading=current_heading,
                    depth=current_depth,
                    body="\n\n".join(current_body),
                    start_page=current_page,
                ))
            current_heading = heading_info["text"]
            current_depth = heading_info["depth"]
            current_page = b.get("page", -1)
            current_body = []
        else:
            current_body.append(text)

    # Save last section
    if current_heading and current_body:
        sections.append(Section(
            heading=current_heading,
            depth=current_depth,
            body="\n\n".join(current_body),
            start_page=current_page,
        ))

    return sections, references


def _detect_heading(text: str, font_size: float, body_size: float) -> Optional[dict]:
    """Detect if a text line is a section heading. Returns {text, depth} or None."""
    stripped = text.strip()

    # Numbered headings: "1. Introduction", "2.1 Method"
    m = NUMBERED_HEADING_RE.match(stripped)
    if m:
        num = m.group(1).rstrip(".")
        heading_text = m.group(2).strip()
        # Determine depth from numbering: "1" = 1, "1.1" = 2, "1.1.1" = 3
        depth = num.count(".") + 1 if "." in num else 1
        return {"text": heading_text, "depth": min(depth, 3)}

    # Roman numeral headings: "I. Introduction", "II. Method"
    rom_match = re.match(r"^([IVXivx]+\.?)\s+(.+)$", stripped)
    if rom_match:
        heading_text = rom_match.group(2).strip()
        if heading_text.lower() in KEYWORD_HEADINGS or font_size > body_size * 1.1:
            return {"text": heading_text, "depth": 1}

    # Keyword-based headings (must be short and match known patterns)
    if stripped.lower() in KEYWORD_HEADINGS and len(stripped) < 40:
        return {"text": stripped, "depth": 1}

    # Font-size based: significantly larger than body
    if font_size > body_size * 1.15 and len(stripped) < 60:
        # Likely a heading — check it's not just a short body line
        if stripped[0].isupper() and not stripped.endswith(","):
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


def _parse_reference_line(text: str, default_idx: int) -> Optional[Reference]:
    """Parse a reference line into a Reference object."""
    text = text.strip()
    if len(text) < 15:
        return None

    # [1] Author, Title...
    m = REF_BRACKET_RE.match(text)
    if m:
        return Reference(
            text=m.group(2).strip(),
            index=int(m.group(1)),
        )

    # 1. Author, Title...
    m = REF_DOT_RE.match(text)
    if m:
        return Reference(
            text=m.group(2).strip(),
            index=int(m.group(1)),
        )

    # Continuation of previous reference or unlabeled reference
    if text[0].isupper() and len(text) > 30:
        return Reference(text=text, index=default_idx)

    return None


def _attach_tables(doc: Document, tables: list):
    """Attach extracted tables to the nearest section."""
    if not tables or not doc.sections:
        return

    table_objects = []
    for t in tables:
        table_objects.append(Table(
            headers=t.get("headers", []),
            rows=t.get("rows", []),
            caption=t.get("caption", ""),
            label=t.get("label", ""),
        ))

    # Distribute tables across sections (simple: evenly or to first section with body)
    for i, table in enumerate(table_objects):
        if doc.sections:
            # Attach to the section at roughly the right position
            idx = min(i, len(doc.sections) - 1)
            doc.sections[idx].tables.append(table)
