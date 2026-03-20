"""
parser/heuristic.py — Stage 2: Convert extracted blocks into a Document.

Two modes:
  A. Font-aware (pymupdf): uses font_size to detect headings (threshold > body)
  B. Text-only (pdfplumber fallback): uses regex patterns for numbered headings

In both modes:
  - Title      = first block / largest font on page 1
  - Abstract   = block(s) after "Abstract" label
  - Authors    = blocks between title and abstract
  - Sections   = headed by detected headings, with depth (1/2/3)
  - References = text after "References" label, split on [N] markers

Public entry point: parse(rich) -> Document
"""
import re
from statistics import median

from core.models import Document, Author, Section, Reference, Table, Figure
from core.logger import get_logger

log = get_logger(__name__)


# ── Known section names ──────────────────────────────────────────────────────

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

# Matches heading patterns: "1 Title", "1. Title", "1.2 Title", "II. Title"
_HEADING_RE = re.compile(
    r"^(?:"
    r"(\d+(?:\.\d+)*\.?)\s+"       # "1 " / "1. " / "2.1 "
    r"|([IVXLCDM]{1,6})[.\s]+"    # "II. " / "IV "
    r")"
    r"([A-Z].*)$"                   # rest = heading text
)


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def parse(rich: dict) -> Document:
    """
    Main entry called by pipeline.py.
    Dispatches to font-aware (Mode A) or text-only (Mode B) parsing.
    """
    blocks = rich.get("blocks", [])
    tables = rich.get("tables", [])
    images = rich.get("images", [])
    doc = Document()

    if not blocks:
        raw = rich.get("raw_text", "")
        blocks = [
            {"text": line.strip(), "font_size": 10, "bold": False, "page": 1}
            for line in raw.split("\n") if line.strip()
        ]

    if not blocks:
        log.error("No content extracted")
        return doc

    # Decide mode: if all font_size == 10 and all bold == False -> text-only
    has_font_info = any(b["font_size"] != 10 or b["bold"] for b in blocks)

    if has_font_info:
        _parse_font_aware(blocks, tables, images, doc)
    else:
        _parse_text_only(blocks, tables, images, doc)

    # Remove any section headed "Abstract" — template handles abstract separately
    doc.sections = [
        s for s in doc.sections
        if s.heading.lower().rstrip(".") != "abstract"
    ]

    # Deduplicate sections (same heading + same body prefix)
    seen = set()
    unique = []
    for s in doc.sections:
        key = (s.heading.lower(), s.body[:200])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    doc.sections = unique

    # Fallback: if no sections at all, dump all body text into one section
    if not doc.sections:
        body = "\n".join(
            b["text"] for b in blocks if b["text"] != doc.title
        )
        doc.sections.append(
            Section(heading="Content", body=body.strip(), depth=1)
        )

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

    # Title = largest font on page 1, but FILTER metadata/junk first
    page1 = [b for b in blocks if b["page"] <= 1] or blocks[:5]
    page1_clean = [
        b for b in page1
        if not _is_metadata_line(b["text"].strip())
        and not _is_table_data_line(b["text"].strip())
        and len(b["text"].strip()) >= 5
        and not re.match(r"^abstract", b["text"].strip(), re.IGNORECASE)
    ]
    if not page1_clean:
        page1_clean = page1  # fallback to unfiltered

    if page1_clean:
        # Score candidates: prefer longer text with large font
        def _title_score(b):
            t = b["text"].strip()
            words = t.split()
            fs = b["font_size"]
            score = fs * 2  # font size matters
            if 4 <= len(words) <= 25:
                score += 5
            if len(words) < 3:
                score -= 10  # penalize very short
            if re.search(r"^\d+\s+\d+$", t):
                score -= 50  # "1 3" type patterns
            if re.search(r"\b(page|vol|doi)\b", t, re.IGNORECASE):
                score -= 50
            if "@" in t or "http" in t.lower():
                score -= 50
            return score

        title_b = max(page1_clean, key=_title_score)
        if title_b["font_size"] >= body_size and _title_score(title_b) > 0:
            doc.title = title_b["text"].strip()

    abstract_block_texts = _detect_abstract_keywords(blocks, doc, threshold)
    _detect_authors_between_title_and_abstract(blocks, doc)

    # Build set of blocks to skip (title, abstract, author lines)
    skip = {doc.title} | abstract_block_texts
    if doc.abstract:
        skip.add(doc.abstract)
    for a in doc.authors:
        if a.name:
            skip.add(a.name)

    # ── Collect sections ─────────────────────────────────────────────────────
    cur_head     = None
    cur_raw_head = ""
    cur_body     = []
    in_refs      = False

    def flush():
        if cur_head and cur_body:
            doc.sections.append(Section(
                heading=cur_head,
                body="\n\n".join(cur_body).strip(),
                depth=_heading_depth(cur_raw_head),
            ))

    for b in blocks:
        t = b["text"].strip()
        if not t or t in skip:
            continue
        if _is_metadata_line(t):
            continue
        if re.match(r"^abstract[\s:—\-]?$", t, re.IGNORECASE):
            continue
        if re.match(r"^(?:keywords?|index terms?)[\s:—\-]", t, re.IGNORECASE):
            continue
        if re.match(r"^references\.?\s*$", t, re.IGNORECASE):
            flush()
            in_refs = True
            continue
        if in_refs:
            continue
        if _is_table_data_line(t):
            continue
        if re.match(r"^Table\s+\d+", t):
            continue

        is_heading = (
            (b["font_size"] >= threshold and len(t) < 100)
            or (b["bold"] and len(t) <= 80 and not re.search(r"[.!?]$", t))
            or (_HEADING_RE.match(t) and len(t) <= 90)
            or (t.lower().rstrip(".") in _SECTION_NAMES
                and t.lower().rstrip(".") != "abstract")
        )

        # Never create a section called "Abstract" or "Authors" — these are
        # metadata, not body sections.  Author data is already extracted by
        # _detect_authors_between_title_and_abstract().
        if is_heading and t.lower().rstrip(".") in {"abstract", "authors", "author"}:
            continue

        if is_heading:
            flush()
            cur_raw_head = t
            cur_head = _strip_num(t)
            cur_body = []
            continue

        if cur_head:
            cur_body.append(t)

    flush()
    _attach_tables(doc, tables, blocks)
    _attach_images(doc, images, blocks)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# MODE B: Text-only parsing (no font info, line-per-block)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_text_only(blocks, tables, images, doc):
    """
    Each block is a single line with no font metadata.
    Walk lines: detect title -> authors -> abstract -> sections -> references.
    """
    lines = [b["text"].strip() for b in blocks if b["text"].strip()]
    if not lines:
        return

    idx   = 0
    total = len(lines)

    # ── Title: scan first ~30 lines, pick best candidate ─────────────────────
    doc.title = ""
    best_title_score = -999
    abstract_limit = min(total, 30)

    for i in range(abstract_limit):
        low = lines[i].lower().strip()
        if low.startswith("abstract"):
            if best_title_score <= -999:
                idx = i
            break
        line = lines[i].strip()
        if _is_metadata_line(line):
            continue
        if len(line) < 5:
            continue
        if (not re.search(r"@|\.\w{2,3}$", line)
                and not re.match(r"^https?://", line)
                and not _is_metadata_line(line)):
            words = line.split()
            score = 0
            if re.search(r"[,·]", line):
                score -= 2
            if re.search(r"\d", line):
                score -= 1
            if "\u202f" in line:
                score -= 5
            if 4 <= len(words) <= 20:
                score += 2
            if line[0].isupper():
                score += 1
            if not re.search(r"\d", line):
                score += 1
            if score > best_title_score:
                best_title_score = score
                doc.title = line
                idx = i + 1

    # ── Title continuation: extend a cut-off title with the next line ────────
    # When a title spans two lines (e.g. "...Framework for Structured" /
    # "Content Extraction and IEEE-Compliant Reconstruction"), the title
    # candidate wins on line 0 but the continuation on line 1 gets
    # misidentified as authors.  Detect continuation: title doesn't end with
    # terminal punctuation AND the next candidate line is not a metadata line,
    # email, URL, or standalone word (authors are typically ≤ 4 words or pass
    # _looks_like_author, so we require the continuation line to have ≥ 3 words
    # and NOT look like an author line).
    if doc.title and idx < total:
        nxt = lines[idx].strip()
        nxt_words = nxt.split()
        _title_terminal = re.search(r"[.!?:)\]\"']$", doc.title)
        if (not _title_terminal
                and not nxt.lower().startswith("abstract")
                and not _is_metadata_line(nxt)
                and not re.search(r"@|https?://", nxt.lower())
                and len(nxt_words) >= 3
                and len(nxt) <= 120
                and not nxt.lower() in {"authors", "author", "keywords", "abstract"}):
            doc.title = doc.title + " " + nxt
            idx += 1

    # ── Authors: lines between title and "Abstract" ──────────────────────────
    current_author = None
    _author_limit = abstract_limit + 60

    while idx < total:
        low = lines[idx].lower().strip()
        if low.startswith("abstract") or re.match(r"^\[\d+\]", lines[idx]):
            break
        # Stop at numbered section headings
        if _HEADING_RE.match(lines[idx].strip()) and _is_heading_line(lines[idx]):
            break
        if idx >= _author_limit:
            break

        line = lines[idx].strip()
        if _is_metadata_line(line):
            idx += 1
            continue

        if "·" in line or (" and " in line and _has_multi_author_pattern(line)):
            parts = re.split(r"\s*[·]\s*|\s+and\s+", line)
            for part in parts:
                part = part.strip()
                if part and _looks_like_author(part):
                    current_author = Author(name=_clean_author_name(part))
                    doc.authors.append(current_author)
            idx += 1
            continue

        if _looks_like_author(line):
            current_author = Author(name=_clean_author_name(line))
            doc.authors.append(current_author)
        elif current_author:
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

    # ── Abstract ─────────────────────────────────────────────────────────────
    if idx < total and lines[idx].lower().strip().startswith("abstract"):
        label = lines[idx]
        m = re.match(r"^abstract[\s:—.\-]+(.+)", label, re.IGNORECASE)
        abs_parts = []
        if m:
            abs_parts.append(m.group(1).strip())
        idx += 1
        while idx < total:
            l = lines[idx]
            if _is_heading_line(l):
                break
            if re.match(r"^(?:keywords?|index terms?)\b", l, re.IGNORECASE):
                break
            if re.match(r"^1[\s.]+[A-Z]", l):
                break
            abs_parts.append(l)
            idx += 1
        if abs_parts:
            doc.abstract = " ".join(abs_parts).strip()

    # ── Keywords ─────────────────────────────────────────────────────────────
    if idx < total:
        m = re.match(
            r"^(?:keywords?|index terms?)[\s:—\-]+(.+)",
            lines[idx], re.IGNORECASE,
        )
        if m:
            doc.keywords = [
                k.strip() for k in re.split(r"[;,·]", m.group(1)) if k.strip()
            ]
            idx += 1

    # ── Sections: walk remaining lines ───────────────────────────────────────
    cur_head     = None
    cur_raw_head = ""
    cur_body     = []
    in_refs      = False
    in_appendix  = False

    def flush():
        if cur_head and cur_body:
            body = _join_body_lines(cur_body)
            if body:
                doc.sections.append(Section(
                    heading=cur_head,
                    body=body,
                    depth=_heading_depth(cur_raw_head),
                ))

    while idx < total:
        line = lines[idx]
        idx += 1

        if re.match(r"^references\.?\s*$", line, re.IGNORECASE):
            flush()
            in_refs = True
            continue
        if in_refs:
            continue
        if _is_metadata_line(line):
            continue
        if re.match(r"^Table\s+\d+", line):
            continue
        if _is_table_data_line(line):
            continue

        # Appendix detection
        if (re.match(r"^Appendix\s+[A-Z]\s*:", line) or
                re.match(r"^Appendix\s*$", line, re.IGNORECASE)):
            flush()
            in_appendix = True
            cur_head = None
            cur_body = []
            continue
        if in_appendix:
            continue

        if _is_heading_line(line):
            heading_text = _strip_num(line)
            # Skip "Authors" — metadata, not a body section
            if heading_text.lower().rstrip(".") in {"authors", "author"}:
                continue
            flush()
            cur_raw_head = line
            cur_head = heading_text
            cur_body = []
            continue
        if cur_head is not None:
            cur_body.append(line)

    flush()
    _attach_tables(doc, tables, blocks)
    _attach_images(doc, images, blocks)
    _extract_refs_from_blocks(blocks, doc)


# ══════════════════════════════════════════════════════════════════════════════
# Heading / depth helpers
# ══════════════════════════════════════════════════════════════════════════════

def _heading_depth(text: str) -> int:
    """
    Infer heading depth from numbering pattern.
      "1 Introduction"    -> 1   "2.1 Related Work" -> 2
      "2.1.1 Aggregation" -> 3   "II. Background"   -> 1
    Capped at 3.
    """
    text = text.strip()
    m = _HEADING_RE.match(text)
    if m and m.group(1):
        num = m.group(1).rstrip(".")
        return min(num.count(".") + 1, 3)
    return 1


def _strip_num(text: str) -> str:
    """Remove leading section number from heading text."""
    m = _HEADING_RE.match(text.strip())
    if m:
        return m.group(3).strip().rstrip(".")
    return text.strip().rstrip(".")


def _is_heading_line(line: str) -> bool:
    """Is this single line a section/subsection heading?"""
    line = line.strip()
    if not line or len(line) > 100:
        return False
    if _is_metadata_line(line):
        return False
    alpha_chars = sum(1 for c in line if c.isalpha())
    if alpha_chars < 3:
        return False

    # Numbered heading
    m = _HEADING_RE.match(line)
    if m:
        heading_text = m.group(3).strip()
        _SENTENCE_STARTERS = {
            "we", "the", "in", "to", "a", "an", "it", "this",
            "our", "these", "that", "there", "if", "for",
        }
        first_word = heading_text.split()[0].lower().rstrip(",;:") if heading_text else ""
        if len(heading_text) <= 70 and first_word not in _SENTENCE_STARTERS:
            return True

    # Known section name alone on a line
    if line.lower().rstrip(".") in _SECTION_NAMES:
        return True

    # ALL CAPS short heading: "INTRODUCTION", "RELATED WORK"
    if (line.isupper()
            and re.match(r"^[A-Z][A-Z\s]+$", line)
            and 3 < len(line) < 60):
        words = line.split()
        if len(words) >= 2 or (len(words) == 1 and len(line) >= 4):
            return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# Metadata / table data filters
# ══════════════════════════════════════════════════════════════════════════════

def _is_metadata_line(line: str) -> bool:
    """Detect journal/page metadata lines that should be skipped."""
    line = line.strip()
    if not line:
        return True

    # Page numbers (various formats)
    if re.search(r"Page\s+\d+\s+of\s+\d+", line, re.IGNORECASE):
        return True
    if re.search(r"\bp\.\s*\d+\s+of\s+\d+", line, re.IGNORECASE):
        return True
    if re.search(r"\d+\s*/\s*\d+\s*$", line) and len(line) < 20:
        return True
    # "72 Page 2 of 36" or "Page 3 of 36 72" (Springer page+article number)
    if re.search(r"\d+\s+Page\s+\d+\s+of\s+\d+", line, re.IGNORECASE):
        return True
    if re.search(r"Page\s+\d+\s+of\s+\d+\s+\d+", line, re.IGNORECASE):
        return True

    # Journal header patterns
    if re.search(r"\(\d{4}\)\s+\d+:\d+", line):
        return True
    if re.search(r"\bVol(?:ume)?\.?\s+\d+", line, re.IGNORECASE):
        return True
    if re.search(r"^\d+\s+Page\s+\d+", line, re.IGNORECASE):
        return True
    if re.search(r"Machine\s+Learning\s*\(\d{4}\)", line, re.IGNORECASE):
        return True
    # Generic journal name + year + volume:page
    if re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+\s+\(\d{4}\)\s+\d+:\d+", line):
        return True

    # Author / affiliation footnotes
    if line.startswith("et al."):
        return True
    if re.match(r"^https?://", line) or "doi.org" in line.lower():
        return True
    if re.match(r"^(Received|Revised|Accepted|Published)\s*:", line, re.IGNORECASE):
        return True
    if "Received:" in line and "Accepted:" in line:
        return True
    if re.match(r"^Editor\s*:", line, re.IGNORECASE):
        return True
    if re.match(r"^Extended author information", line, re.IGNORECASE):
        return True
    if re.match(r"^Corresponding author", line, re.IGNORECASE):
        return True
    if re.search(r":\s*The work was done at", line):
        return True
    if re.match(r"^Authors and Affiliations", line, re.IGNORECASE):
        return True
    if re.match(r"^Publisher'?s?\s+Note", line, re.IGNORECASE):
        return True

    # Copyright / licensing
    if "\u00a9" in line or "copyright" in line.lower():
        return True
    if "creative commons" in line.lower() or "cc by" in line.lower():
        return True
    if re.search(r"\b(CC\s+BY|Attribution)\b.*licen", line, re.IGNORECASE):
        return True
    if "springer nature" in line.lower() or "open access" in line.lower():
        return True

    # Pure page-number lines
    if re.match(r"^\d{1,4}$", line):
        return True
    # Springer narrow no-break space markers
    if ("\u202f" in line and len(line) <= 10
            and all(c.isdigit() or c.isspace() for c in line)):
        return True
    # "1 3" or "72 3" — short digit-only fragments (Springer page markers)
    if re.match(r"^\d{1,4}(\s+\d{1,4})*$", line) and len(line) <= 15:
        return True
    # "– N" or "N –" page range fragments
    if re.match(r"^[\d\s\u2013\u2014\-]+$", line) and len(line) <= 15:
        return True

    # "Article N" / "Paper N" markers
    if re.match(r"^(Article|Paper)\s+\d+$", line, re.IGNORECASE):
        return True

    return False


def _is_table_data_line(line: str) -> bool:
    """Detect lines that are table data rows — skip during body collection."""
    line = line.strip()
    if not line:
        return False

    # Arrow symbols (significance markers in table cells/captions)
    _ARROWS = "\u2191\u2193\u21d1\u21d3\u2197\u2198"
    arrow_count = sum(1 for c in line if c in _ARROWS)
    if arrow_count > 0 and not re.search(r"[a-zA-Z]", line):
        return True
    if line[0] in _ARROWS:
        return True
    if arrow_count >= 2:
        return True
    if re.search(r"[\d.][" + _ARROWS + r"]|[" + _ARROWS + r"][\d.]", line):
        return True

    # Table caption continuation fragments
    if re.match(r"^similarly\s+to\s+(Table|Tab\.?)\s+\d+", line, re.IGNORECASE):
        return True
    if re.match(r"^(significantly\s+(worse|better)|results?\s+similarly)\b",
                line, re.IGNORECASE):
        return True
    if re.search(r"\bdenote\s+significantly\b", line, re.IGNORECASE):
        return True
    if re.search(r"\bp[<>]=?\s*0\.\d+\s+and\s+p[<>]=?\s*0\.\d+", line):
        return True
    if line.lower() in ("ours", "ours.", "our", "our.", "similarly", "results"):
        return True

    # TABLE + Roman numeral caption lines ("TABLE III", "TABLE IV")
    if re.match(r"^TABLE\s+[IVXLCDM]+", line):
        return True
    # "Table N:" continuation or "Tab. N"
    if re.match(r"^Tab(?:le)?\.?\s+\d+\b", line, re.IGNORECASE):
        return True

    # "#Clients" header rows
    if re.match(r"^#\w+\b", line):
        return True

    # 3+ distinct numeric tokens (table rows typically have many numbers)
    num_tokens = re.findall(r"\b\d[\d./%\u00b1\u2213]*\b", line)
    if len(num_tokens) >= 4:
        return True
    # 3 numbers with a label prefix (method name + results)
    if len(num_tokens) >= 3 and re.match(r"^[A-Za-z]", line):
        alpha_ratio = sum(1 for c in line if c.isalpha()) / max(len(line), 1)
        if alpha_ratio < 0.4:
            return True

    # Pure dashes/spaces (table empty-cell placeholders)
    if re.match(r"^[\s\-\u2212\u2013\u2014N/Aa]+$", line) and len(line) <= 20:
        return True

    # Mostly digits/punctuation
    stripped = re.sub(r"[\d.\s\-/\u2212\u2013\u2014]", "", line)
    if len(line) > 5 and len(stripped) < len(line) * 0.3:
        return True

    # Method/row name + 3+ numbers
    if re.match(r"^[\w\s\-+.]+\s+([\d.]+\s+){2,}[\d.]+\s*$", line):
        return True
    if re.match(r"^\w+\s+[\d.]+(\s+[\d.]+){2,}", line):
        return True

    # All-caps column headers ("RMSE ECE MAP")
    if re.match(r"^([A-Z]{3,}\s+){2,}", line):
        return True

    # 4+ capitalised tokens, no sentence starters
    tokens = line.split()
    if len(tokens) >= 4:
        cap_count = sum(1 for t in tokens if t and t[0].isupper())
        if cap_count == len(tokens):
            _starters = {
                "The", "This", "For", "In", "We", "Our", "To", "A", "An",
                "And", "It", "Such", "With", "As", "By", "At", "From",
                "Each", "All", "Some", "Both",
            }
            _conj = {"and", "or", "vs", "v.s.", "versus"}
            if (not any(t in _starters for t in tokens)
                    and not any(t.lower() in _conj for t in tokens)):
                return True

    # Pure integer rows
    if all(re.match(r"^\d+$", t) for t in tokens) and len(tokens) >= 2:
        return True

    # Legacy patterns
    if all(t.lower() in ("long", "medium", "short") for t in tokens):
        return True
    if re.match(r"^(?:Benchmark|Speedup)\b", line, re.IGNORECASE):
        return True
    if line.lower() in ("livermore", "clinpack", "spec", "pass"):
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# Body / author / abstract helpers
# ══════════════════════════════════════════════════════════════════════════════

def _join_body_lines(lines: list) -> str:
    """Join consecutive body lines into paragraphs."""
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
    clean = re.sub(r"[*\u2020\u2021\u00a7\d,\u00b7]+$", "", text).strip()
    clean = re.sub(r"\d+", "", clean).strip()
    clean = re.sub(r"\s*[\u00b7]\s*", " ", clean).strip()
    words = [w for w in clean.split() if w]
    if not 2 <= len(words) <= 5:
        return False
    if not all(w[0].isupper() for w in words if w):
        return False
    if all(w.isupper() for w in words):
        return False
    # Reject if any word is an all-caps acronym (e.g. "IEEE", "AI", "NLP")
    # Real author names don't normally contain all-caps acronyms of length ≥ 3.
    if any(w.isupper() and len(w) >= 3 for w in words):
        return False
    # Reject if any word contains a hyphen — title/technical phrases like
    # "IEEE-Compliant" or "Deep-Learning" are not author names.
    if any("-" in w for w in words):
        return False
    low = text.lower()
    reject_keywords = [
        "department", "university", "institute", "school",
        "college", "inc", "corp", "lab", "formula",
        "abstract", "introduction", "conclusion",
        "received:", "accepted:", "revised:",
        # Section heading words that form 2-3 word capitalized phrases
        "experiment", "results", "method", "approach", "analysis",
        "discussion", "evaluation", "framework", "architecture",
        "background", "overview", "motivation", "implementation",
        "appendix", "setting", "privacy", "attack", "messages",
        "sensitivity", "sensitivities", "details", "additional",
        "generalization", "personalization", "differential",
        "adversarial", "variance", "kernel", "approximate",
        "stationary", "non-stationary", "unifying", "random",
        "federated", "regression", "bayesian", "theorem",
    ]
    if any(kw in low for kw in reject_keywords):
        return False
    # Reject if it matches a known section name
    if clean.lower().rstrip(".") in _SECTION_NAMES:
        return False
    # Reject if it looks like a numbered heading
    if _HEADING_RE.match(text.strip()):
        return False
    return True


def _clean_author_name(text: str) -> str:
    """Strip superscript digits, markers from author name."""
    clean = re.sub(r"[*\u2020\u2021\u00a7]+", "", text).strip()
    clean = re.sub(r"\d+,?\d*$", "", clean).strip()
    clean = re.sub(r"(\w)\d+", r"\1", clean)
    return clean.strip()


def _has_multi_author_pattern(line: str) -> bool:
    parts = re.split(r"\s+and\s+", line)
    return (len(parts) >= 2
            and all(_looks_like_author(p.strip()) for p in parts if p.strip()))


def _looks_like_department(text: str) -> bool:
    low = text.lower().strip()
    return any(kw in low for kw in [
        "department", "dept", "faculty", "school of", "division of", "group of",
    ])


def _looks_like_organization(text: str) -> bool:
    low = text.lower().strip()
    return any(kw in low for kw in [
        "university", "institute", "college", "laboratory", "lab",
        "inc.", "corp", "ltd", "company", "research center", "polytechnic",
    ])


def _looks_like_location(text: str) -> bool:
    text = text.strip()
    return bool(re.match(r"^[A-Z][a-z]+.*,\s*[A-Z]", text))


def _looks_like_email(text: str) -> bool:
    return bool(re.search(r"[\w.+-]+@[\w.-]+\.\w+", text.strip()))


# ══════════════════════════════════════════════════════════════════════════════
# Font-aware: abstract + author detection
# ══════════════════════════════════════════════════════════════════════════════

def _detect_abstract_keywords(blocks, doc, threshold):
    """Detect abstract and keywords from blocks (font-aware mode)."""
    abstract_block_texts = set()
    abs_collecting = False
    abs_parts = []

    for b in blocks:
        t = b["text"].strip()
        low = t.lower()

        # Keywords line
        m_kw = re.match(
            r"^(?:keywords?|index terms?)[\s:—\-]+(.+)",
            t, re.IGNORECASE | re.DOTALL,
        )
        if m_kw:
            if abs_collecting:
                abs_collecting = False
            doc.keywords = [
                k.strip() for k in re.split(r"[;,·]", m_kw.group(1)) if k.strip()
            ]
            abstract_block_texts.add(t)
            continue

        if low == "abstract" or re.match(r"^abstract[\s.]*$", low):
            abs_collecting = True
            abstract_block_texts.add(t)
            continue

        m_abs = re.match(r"^abstract[\s:—.\-]+(.+)", t, re.IGNORECASE | re.DOTALL)
        if m_abs and not doc.abstract:
            abs_parts.append(m_abs.group(1).strip())
            abs_collecting = True
            abstract_block_texts.add(t)
            continue

        if abs_collecting:
            is_heading = (
                (b["font_size"] >= threshold and len(t) < 100)
                or (b.get("bold", False) and len(t) <= 80
                    and not re.search(r"[.!?]$", t))
                or (_HEADING_RE.match(t) and len(t) <= 90)
                or (t.lower().rstrip(".") in _SECTION_NAMES
                    and t.lower().rstrip(".") != "abstract")
            )
            if is_heading:
                abs_collecting = False
            else:
                abs_parts.append(t)
                abstract_block_texts.add(t)
                continue

    if abs_parts and not doc.abstract:
        doc.abstract = " ".join(abs_parts).strip()

    return abstract_block_texts


def _detect_authors_between_title_and_abstract(blocks, doc):
    """Font-aware: blocks between title and abstract = authors + affiliations.

    Handles:
    - "Authors" label blocks (skipped)
    - Multi-line PyMuPDF blocks (split on newlines and process each line)
    """
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
        # Skip standalone "Authors" / "Author" label
        if re.match(r"^authors?\s*$", t, re.IGNORECASE):
            continue
        # PyMuPDF may combine multiple lines into one block — split and
        # process each line individually.
        for line in t.split("\n"):
            line = line.strip()
            if not line:
                continue
            if re.match(r"^abstract", line, re.IGNORECASE):
                break
            if re.match(r"^authors?\s*$", line, re.IGNORECASE):
                continue
            if _looks_like_author(line):
                current_author = Author(name=_clean_author_name(line))
                doc.authors.append(current_author)
            elif current_author:
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


# ══════════════════════════════════════════════════════════════════════════════
# Table / image / reference attachment
# ══════════════════════════════════════════════════════════════════════════════

def _attach_tables(doc, tables_raw, blocks):
    """Attach extracted tables to the most relevant section."""
    if not tables_raw and blocks:
        tables_raw = _extract_tables_from_text(blocks)

    if not tables_raw or not doc.sections:
        return

    # Filter out obviously bad tables before attaching
    valid_tables = []
    for tbl_raw in tables_raw:
        headers = tbl_raw.get("headers", [])
        rows = tbl_raw.get("rows", [])
        # Skip tables with no real data
        if not headers and not rows:
            continue
        # Skip if header is a single cell with lots of text (body text leak)
        if len(headers) == 1 and len(headers[0]) > 100:
            log.debug("Skipping table with body-text header: %s", headers[0][:60])
            continue
        # Skip if header contains many LaTeX math fragments
        header_text = " ".join(headers)
        if header_text.count("$") > 6:
            log.debug("Skipping table with math-heavy header")
            continue
        valid_tables.append(tbl_raw)

    tables_raw = valid_tables
    if not tables_raw:
        return

    for tbl_idx, tbl_raw in enumerate(tables_raw, start=1):
        headers = [str(c or "").strip() for c in tbl_raw.get("headers", [])]
        rows = [[str(c or "").strip() for c in row] for row in tbl_raw.get("rows", [])]
        caption = tbl_raw.get("caption", "")
        notes = tbl_raw.get("notes", "")
        tbl = Table(caption=caption, headers=headers, rows=rows, notes=notes)

        attached = False
        # Try to extract table number from caption
        tbl_num_m = re.match(r"^.*?(?:Table|TABLE)\s*(\d+|[IVXLCDM]+)",
                             caption) if caption else None
        tbl_ref = None
        if tbl_num_m:
            tbl_ref = f"table {tbl_num_m.group(1).lower()}"
        else:
            # Use sequential numbering as fallback
            tbl_ref = f"table {tbl_idx}"

        if tbl_ref:
            for section in doc.sections:
                if tbl_ref in section.body.lower():
                    section.tables.append(tbl)
                    attached = True
                    break

        if not attached:
            # Attach to the last section before Conclusion
            target = doc.sections[-1]
            for s in doc.sections:
                if s.heading.lower() in ("conclusion", "conclusions", "future work"):
                    break
                target = s
            target.tables.append(tbl)

    log.info("Attached %d table(s) to sections", len(tables_raw))


def _attach_images(doc, images_raw, blocks):
    """Attach extracted images to sections based on page proximity."""
    if not images_raw or not doc.sections:
        return

    section_pages = {}
    if blocks:
        for i, sec in enumerate(doc.sections):
            for b in blocks:
                if (b["text"].strip() == sec.heading
                        or sec.heading in b["text"]):
                    section_pages[i] = b.get("page", 1)
                    break

    fig_captions = {}
    for b in blocks:
        t = b["text"].strip()
        m = re.match(r"^(?:Fig\.?|Figure)\s*(\d+)\b\s*(.*)", t, re.IGNORECASE)
        if m:
            fig_num = int(m.group(1))
            fig_captions[fig_num] = {
                "caption": m.group(2).strip(),
                "page": b.get("page", 1),
            }

    for idx, img in enumerate(images_raw):
        img_page = img.get("page", 1)
        fig_num = idx + 1
        caption = fig_captions.get(fig_num, {}).get("caption", "")

        fig = Figure(
            caption=caption,
            image_path=img.get("path", ""),
            label=f"fig:{fig_num}",
        )

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
    """Extract references from blocks after 'References' heading."""
    in_refs = False
    ref_texts = []
    current_ref_parts = []
    current_idx = 0

    for b in blocks:
        text = b["text"].strip()
        if not text:
            continue
        if re.match(r"^references\.?\s*$", text, re.IGNORECASE):
            in_refs = True
            continue
        if not in_refs:
            continue

        # [N] bracket marker  → "[1] Author..."
        m = re.match(r"^\[(\d+)\]\s*(.*)", text)
        # N. period marker    → "1. Author..." (common in many IEEE/NeurIPS papers)
        # Require the text after "N." to be ≥ 20 chars to avoid matching
        # stray page-number lines like "202. Springer."
        if not m:
            m2 = re.match(r"^(\d+)\.\s+(.*)", text)
            if m2 and len(m2.group(2)) >= 20:
                m = m2
        if m:
            if current_ref_parts and current_idx:
                ref_texts.append((current_idx, " ".join(current_ref_parts)))
            current_idx = int(m.group(1))
            current_ref_parts = [m.group(2).strip()] if m.group(2).strip() else []
        else:
            current_ref_parts.append(text)

    if current_ref_parts and current_idx:
        ref_texts.append((current_idx, " ".join(current_ref_parts)))

    # Fallback: author-year style (no [N] or N. markers found).
    # Only scan blocks AFTER the "References" heading to avoid false positives
    # from the paper body.
    if not ref_texts and in_refs:
        idx = 1
        after_refs = False
        for b in blocks:
            text = b["text"].strip()
            if not text:
                continue
            if re.match(r"^references\.?\s*$", text, re.IGNORECASE):
                after_refs = True
                continue
            if not after_refs:
                continue
            if re.match(r"^[A-Z]", text) and re.search(r"\b(19|20)\d{2}\b", text):
                ref_texts.append((idx, text))
                idx += 1

    # Build Reference objects
    for idx_val, text in ref_texts:
        doc.references.append(Reference(index=idx_val, text=text))

    # Clean boilerplate that leaks into refs
    clean_refs = []
    for r in doc.references:
        text = r.text
        if re.match(r"^(Authors and Affiliations|Open Access)\b", text, re.IGNORECASE):
            continue
        if re.search(r"\b(Springer Nature|Elsevier)\b.*licen", text, re.IGNORECASE):
            continue
        if re.match(r"^Open Access\b", text, re.IGNORECASE):
            continue
        if (re.search(r"\b(University|Institute|Lab|Huawei|Google|Microsoft|DeepMind)\b", text)
                and len(text.split()) < 20):
            continue
        clean_refs.append(r)
    doc.references = clean_refs

    # Deduplicate by index
    seen = set()
    unique = []
    for r in sorted(doc.references, key=lambda x: x.index):
        if r.index not in seen:
            seen.add(r.index)
            unique.append(r)
    doc.references = unique

    log.info("Extracted %d references", len(doc.references))


def _extract_tables_from_text(blocks: list) -> list:
    """Extract tables from raw text blocks when pdfplumber finds none."""
    tables = []
    i = 0
    while i < len(blocks):
        t = blocks[i]["text"].strip()
        # Match "Table N" or "TABLE N" (Arabic or Roman numerals)
        m = re.match(r"^(?:Table|TABLE)\s+(\d+|[IVXLCDM]+)\b\s*(.*)", t)
        if not m:
            i += 1
            continue

        caption_parts = [m.group(2).strip()] if m.group(2).strip() else []
        i += 1

        # Collect caption continuation lines (max 5 lines of caption)
        cap_count = 0
        while i < len(blocks) and cap_count < 5:
            t2 = blocks[i]["text"].strip()
            if re.match(r"^[\u21d1\u21d3\u2191\u2193\s,]+$", t2):
                i += 1
                continue
            if _looks_like_table_row(t2):
                break
            if _is_heading_line(t2):
                break
            if _is_metadata_line(t2):
                i += 1
                continue
            # Stop caption if line is too long (likely body text)
            if len(t2) > 200:
                break
            caption_parts.append(t2)
            cap_count += 1
            i += 1

        caption = re.sub(r"\s+", " ", " ".join(caption_parts).strip())
        headers = []
        rows = []
        max_table_rows = 30  # safety limit

        # Collect table rows
        while i < len(blocks) and len(rows) < max_table_rows:
            t2 = blocks[i]["text"].strip()
            if _is_metadata_line(t2):
                i += 1
                continue
            if re.match(r"^[\u21d1\u21d3\u2191\u2193\s,]+$", t2):
                i += 1
                continue
            if re.match(r"^(?:Table|TABLE)\s+(\d+|[IVXLCDM]+)\b", t2):
                break
            if _is_heading_line(t2):
                break
            if re.match(r"^references\.?\s*$", t2, re.IGNORECASE):
                break
            if len(t2) < 3:
                i += 1
                continue
            # Stop if we hit normal prose (long line with few numbers)
            if len(t2) > 150 and not _looks_like_table_row(t2):
                break
            if _looks_like_table_row(t2):
                cells = _split_table_row(t2)
                if not headers:
                    headers = cells
                else:
                    rows.append(cells)
            else:
                # Non-table line in the middle of a table — stop collecting
                if headers or rows:
                    break
            i += 1

        # Validate table: needs at least 2 columns and reasonable structure
        if _validate_table(headers, rows):
            tables.append({
                "caption": caption[:300],  # cap caption length
                "headers": headers,
                "rows":    rows,
                "notes":   "",
            })

    return tables


def _validate_table(headers: list, rows: list) -> bool:
    """Validate that extracted table data looks reasonable."""
    if not headers and not rows:
        return False
    # Header shouldn't be a formula or long prose
    if headers:
        header_text = " ".join(headers)
        if len(header_text) > 300:
            return False
        # Reject if header contains many LaTeX math (body text leak)
        if header_text.count("$") > 6:
            return False
        # Reject if header looks like a sentence (prose, not column names)
        if re.search(r"\b(which|that|this|these|is|are|was|were)\b",
                     header_text, re.IGNORECASE) and len(header_text) > 80:
            return False
    # Need at least 1 row
    if not rows:
        return False
    return True


def _looks_like_table_row(line: str) -> bool:
    """Heuristic: line has 3+ whitespace-separated tokens with numbers or short labels."""
    tokens = line.split()
    if len(tokens) < 3:
        return False
    # Count numeric-like tokens (including fractions like 2670/334, percentages)
    num_count = sum(1 for t in tokens
                    if re.match(r"^[\d./]+[⇑⇓↑↓%±]*$", t))
    # Need at least 2 numbers, and the line shouldn't be too long (prose)
    if num_count >= 2 and len(line) < 200:
        return True
    # Also match: short capitalized labels with no sentences (column headers)
    # e.g., "Skillcraft SML Parkinsons Bike CCPP"
    if (len(tokens) >= 3 and len(tokens) <= 12
            and all(len(t) <= 20 for t in tokens)
            and len(line) < 100
            and not re.search(r"[.!?;]$", line.strip())
            and all(t[0].isupper() or t.startswith("#") for t in tokens if t)):
        return True
    return False


def _split_table_row(line: str) -> list:
    """
    Split a table row into cells.
    Try 2+ spaces first. If that gives only 1 cell, fall back to detecting
    the label prefix vs numeric data.
    """
    # First try: split on 2+ spaces
    cells = re.split(r"\s{2,}", line.strip())
    cells = [c.strip() for c in cells if c.strip()]
    if len(cells) >= 2:
        return cells

    # Second try: split "MethodName 0.95 0.21 ..." into [label, n1, n2, ...]
    # Find where numeric data starts
    m = re.match(r"^([A-Za-z][A-Za-z\s\-+.]*?)\s+([\d.]+(?:[⇑⇓↑↓]*)(?:\s+[\d.]+[⇑⇓↑↓]*)*)\s*$",
                 line.strip())
    if m:
        label = m.group(1).strip()
        numbers = m.group(2).strip().split()
        return [label] + numbers

    # Third try: just split on any whitespace
    tokens = line.strip().split()
    return tokens
