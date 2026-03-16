"""
stages/document_parser.py
Stage 2 — raw text blocks → structured Document model

Handles the real PyMuPDF extraction format where:
  - Section headings arrive as "1\nIntroduction" (number + name in one block)
  - Author block is multi-line: name / dept / org / city / email
  - Abstract label may be a standalone block followed by text in the next block
  - References arrive as one large block with [1] [2] markers inline
"""

import re
from core.models import Document, Section, Author


# ── Known section names (lowercased, numbering stripped) ─────────────────────
IEEE_SECTION_NAMES = {
    "abstract", "introduction", "related work", "background",
    "methodology", "methods", "approach", "system design",
    "implementation", "experiments", "experimental study",
    "evaluation", "results", "discussion",
    "conclusion", "conclusions", "future work",
    "acknowledgment", "acknowledgements", "references", "bibliography",
}

# Matches leading section numbers like "1", "2.1", "I.", "II." etc.
_LEADING_NUM = re.compile(r"^(\d+(\.\d+)*|[ivxlcdmIVXLCDM]+)\.?\s*")


def parse(extracted_path: str) -> Document:
    with open(extracted_path, encoding="utf-8") as f:
        raw = f.read()

    # Split on blank lines to get blocks
    blocks = [b.strip() for b in raw.split("\n\n") if b.strip()]
    # Also pre-join any lone number block with the next block
    # e.g. ["1", "Introduction", ...] → ["1\nIntroduction", ...]
    blocks = _merge_orphan_numbers(blocks)

    doc = Document()
    doc.title   = _find_title(blocks)
    doc.authors = _find_authors(blocks, doc.title)
    _parse_body(blocks, doc)
    return doc


# ── Pre-processing ────────────────────────────────────────────────────────────

def _merge_orphan_numbers(blocks: list[str]) -> list[str]:
    """
    PyMuPDF sometimes emits a lone section number as its own block.
    e.g.  blocks = ["1", "Introduction", ...]
    Merge them so heading detection sees "1\nIntroduction".
    """
    merged = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        # A block that is ONLY a number (possibly with a dot)
        if re.match(r"^\d+(\.\d+)?\.?$", b) and i + 1 < len(blocks):
            merged.append(b + "\n" + blocks[i + 1])
            i += 2
        else:
            merged.append(b)
            i += 1
    return merged


# ── Field extractors ──────────────────────────────────────────────────────────

def _find_title(blocks: list[str]) -> str:
    for b in blocks:
        if b.strip():
            # Take only the first line of the first block (title is never multi-line)
            return b.strip().split("\n")[0].strip()
    return "Untitled"


def _find_authors(blocks: list[str], title: str) -> list[Author]:
    """
    The author block typically appears right after the title and contains
    the author name on line 1, then dept / org / city / email on subsequent lines.

    Example block:
        Cindy Norris
        Department of Computer Science
        Appalachian State University
        Boone, NC 28608
        (828)262-2359
        can@cs.appstate.edu
    """
    for block in blocks[1:6]:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines or block == title:
            continue

        first = lines[0]
        # Must look like a person's name: two+ capitalised words, short
        if not re.match(r"^[A-Z][a-z]+([\s\-][A-Z][a-z]+)+$", first):
            continue

        author = Author(name=first)

        for line in lines[1:]:
            low = line.lower()
            if "@" in line or re.match(r"[\w.+-]+@[\w.-]+", line):
                author.email = line
            elif re.match(r"^(dept|department|school|faculty|div)", low):
                author.department = line
            elif re.match(r"^\d[\d\s\-()]+$", line):
                pass   # phone number — skip
            elif not author.organization and re.search(r"(university|institute|college|lab|inc\.|ltd)", low):
                author.organization = line
            elif not author.city and re.match(r"^[A-Z][a-zA-Z\s]+,?\s*[A-Z]{2}[\s\d]*$", line):
                author.city = line
            elif not author.organization:
                author.organization = line

        return [author]
    return []


# ── Body parser ───────────────────────────────────────────────────────────────

def _parse_body(blocks: list[str], doc: Document):
    current_heading: str | None = None
    current_body: list[str] = []
    next_is_abstract = False   # flag: standalone "Abstract" block seen
    in_references = False

    author_names = {a.name for a in doc.authors}

    def _flush():
        if current_heading and current_body:
            doc.sections.append(Section(
                heading=current_heading,
                body="\n\n".join(current_body).strip()
            ))

    for block in blocks:
        lower = block.lower().strip()
        first_line = block.split("\n")[0].strip()

        # ── Skip title / author blocks ─────────────────────────────────────────
        if first_line == doc.title or first_line in author_names:
            continue

        # ── Standalone "Abstract" label ────────────────────────────────────────
        if lower == "abstract":
            next_is_abstract = True
            continue

        if next_is_abstract:
            doc.abstract = block.strip()
            next_is_abstract = False
            continue

        # ── Inline "Abstract — text..." ───────────────────────────────────────
        if re.match(r"^abstract[\s:—\-]+\S", block, re.IGNORECASE) and not doc.abstract:
            doc.abstract = re.sub(
                r"^abstract[\s:—\-]+", "", block, flags=re.IGNORECASE
            ).strip()
            continue

        # ── Keywords ──────────────────────────────────────────────────────────
        if re.match(r"^(keywords?|index terms?)[\s:—\-]", lower):
            kw_text = re.sub(
                r"^(keywords?|index terms?)[\s:—\-]+", "", block, flags=re.IGNORECASE
            )
            doc.keywords = [k.strip() for k in re.split(r"[;,]", kw_text) if k.strip()]
            continue

        # ── References block ──────────────────────────────────────────────────
        # Could be a standalone "References" label...
        if re.match(r"^references\.?$", lower):
            _flush()
            current_heading = None
            current_body = []
            in_references = True
            continue

        # ...or subsequent blocks while inside references
        if in_references:
            parts = _split_references(block)
            for part in parts:
                # Strip leading [N] marker from the text — \bibitem already adds the number
                part = re.sub(r"^\[\d+\]\s*", "", part).strip()
                if not part:
                    continue
                # If this part looks like a continuation (no capital letter start typical
                # of a new author name) append to last ref
                if doc.references and not re.match(r"^[A-Z\[]", part):
                    doc.references[-1] = doc.references[-1] + " " + part
                else:
                    doc.references.append(part)
            continue

        # ── Section heading ───────────────────────────────────────────────────
        heading = _detect_heading(block)
        if heading:
            _flush()
            current_heading = heading
            current_body = []
            continue

        # ── Body text ─────────────────────────────────────────────────────────
        # Skip blocks that look like raw table data (rows of numbers/columns)
        if _is_table_data(block):
            continue
        if current_heading is not None:
            current_body.append(block.strip())

    _flush()


# ── Heading detection ─────────────────────────────────────────────────────────

def _detect_heading(block: str) -> str | None:
    """
    Returns the clean heading string if the block is a section heading, else None.

    Handles these formats from real PDFs:
      "1\nIntroduction"          → "Introduction"
      "2.1\nBackground"          → "Background"
      "Introduction"             → "Introduction"
      "INTRODUCTION"             → "Introduction"
      "3 Experimental Study"     → "Experimental Study"
    """
    stripped = block.strip()

    # Multi-line: first line is a number, second line is the heading text
    lines = stripped.split("\n")
    if len(lines) == 2:
        num, text = lines[0].strip(), lines[1].strip()
        if re.match(r"^\d+(\.\d+)*\.?$|^[ivxlcdmIVXLCDM]+\.?$", num, re.IGNORECASE):
            clean = _LEADING_NUM.sub("", text).strip().rstrip(".")
            if clean.lower() in IEEE_SECTION_NAMES or (text.isupper() and len(text) < 60):
                return clean

    # Single line with optional leading number: "3 Experimental Study"
    if len(stripped) <= 80:
        clean = _LEADING_NUM.sub("", stripped).strip().rstrip(".")
        if clean.lower() in IEEE_SECTION_NAMES:
            return clean
        # ALL CAPS short line
        if stripped.isupper() and 3 < len(stripped) < 60:
            return stripped.title()

    return None


# ── Reference splitting ───────────────────────────────────────────────────────

def _is_table_data(block: str) -> bool:
    """
    Detect blocks that are raw table data extracted from PDFs.
    PyMuPDF often extracts table rows as lines of space-separated numbers,
    or as individual numbers one per line.
    We skip these — they can't be reconstructed as LaTeX tables from plain text.
    """
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    if len(lines) < 3:
        return False

    numeric_lines = sum(
        1 for l in lines
        # Line is mostly numbers/decimals with little alphabetic content
        if re.match(r"^[\d\s.]+$", l) or
           (len(re.findall(r"[\d.]+", l)) >= 2 and len(re.findall(r"[a-zA-Z]", l)) <= 4)
    )
    return numeric_lines >= len(lines) * 0.55


def _split_references(block: str) -> list[str]:
    """
    Split a references block into individual entries.
    Each block may be:
      - A full entry:       "[1] Author, Title..."
      - A continuation:     "for registers and functional units..."
      - Multiple entries:   "[1] ... [2] ..."
    """
    stripped = re.sub(r"^references[\s\n]*", "", block, flags=re.IGNORECASE).strip()
    if not stripped:
        return []

    # Multiple bracketed entries in one block
    parts = re.split(r"(?=\[\d+\])", stripped)
    parts = [" ".join(p.split()) for p in parts if p.strip()]
    if len(parts) > 1:
        return parts

    # Single entry or continuation — return as-is (collapsed)
    return [" ".join(stripped.split())]