"""
stages/document_parser.py
Stage 2 — raw text blocks → structured Document model

Key insight from real PyMuPDF extraction:
  - Every LINE is its own double-newline-separated block
  - Paragraphs must be re-joined by detecting sentence continuations
  - Section numbers arrive as lone blocks: "1", "2.1" etc
  - Table data is a cluster of number-only blocks
  - Abstract follows a standalone "Abstract" label block
  - References each span multiple continuation blocks
"""

import re
from core.models import Document, Section, Author


# ── Known IEEE section names ──────────────────────────────────────────────────
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

def parse(extracted_path: str) -> Document:
    with open(extracted_path, encoding="utf-8") as f:
        raw = f.read()

    # Step 1: raw lines → logical blocks
    blocks = _build_blocks(raw)

    doc = Document()
    doc.title   = _find_title(blocks)
    doc.authors = _find_authors(blocks, doc.title)
    _parse_body(blocks, doc)
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Block builder  (the key pre-processing step)
# ─────────────────────────────────────────────────────────────────────────────

def _build_blocks(raw: str) -> list[str]:
    """
    Convert raw extracted text into logical blocks.
    """
    chunks = [c.strip() for c in raw.split("\n\n") if c.strip()]
    chunks = _merge_orphan_numbers(chunks)

    # First pass: cluster table data into single blocks
    chunks = _cluster_tables(chunks)

    blocks: list[str] = []
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

        # Never merge already-clustered table blocks
        if prev.startswith("__TABLE__") or chunk.startswith("__TABLE__"):
            blocks.append(chunk)
            continue

        # Never merge image markers
        if chunk.startswith("__IMAGE__") or prev.startswith("__IMAGE__"):
            blocks.append(chunk)
            continue

        # Never merge equation blocks
        if chunk.startswith("$$") or prev.startswith("$$"):
            blocks.append(chunk)
            continue

        # Never merge DOCX table blocks
        if chunk.startswith("DOCX_TABLE") or prev.startswith("DOCX_TABLE"):
            blocks.append(chunk)
            continue

        if _is_metadata_line(chunk) or _is_metadata_line(prev):
            blocks.append(chunk)
            continue

        if _is_continuation(prev, chunk):
            blocks[-1] = prev + " " + chunk
        else:
            blocks.append(chunk)

    # Strip the __TABLE__ sentinel
    return [b[9:] if b.startswith("__TABLE__") else b for b in blocks]


def _cluster_tables(chunks: list[str]) -> list[str]:
    """
    Find table data clusters and collapse them into a single __TABLE__ block.
    Looks up to 40 chunks ahead for a Table caption.
    Handles captions like "Table 1", "Table 1:", "Table 1 Caption text".
    """
    result = []
    i = 0
    while i < len(chunks):
        # Look for a "Table N" caption within next 40 chunks
        caption_offset = None
        for j in range(i, min(i + 40, len(chunks))):
            if re.match(r"^Table\s+\d+", chunks[j], re.IGNORECASE):
                caption_offset = j
                break

        if caption_offset is not None and caption_offset > i:
            span = chunks[i:caption_offset + 1]
            all_text = "\n".join(span)
            # Must contain numbers (data) and be reasonably long
            num_count = len(re.findall(r"\b\d+\.?\d*\b", all_text))
            if num_count >= 3 and len(span) >= 2:
                # Skip leading body-text chunk if it looks like prose
                first = span[0]
                if len(first.split()) > 6 and re.search(r"[.!?]", first):
                    result.append(first)
                    merged = "\n".join(span[1:])
                else:
                    merged = "\n".join(span)
                result.append("__TABLE__" + merged)
                i = caption_offset + 1
                continue

        result.append(chunks[i])
        i += 1
    return result


def _is_metadata_line(line: str) -> bool:
    """Single-line blocks that are author metadata — never merge these."""
    line = line.strip()
    if "\n" in line:
        return False
    # Email
    if "@" in line:
        return True
    # Phone
    if re.match(r"^\(?\d[\d\s\-().]{5,}$", line):
        return True
    # City/State like "Boone, NC 28608"
    if re.match(r"^[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{0,5}$", line):
        return True
    # Dept / org (short, no sentence punctuation)
    if len(line) < 60 and not re.search(r"[.!?]$", line) and re.match(r"^[A-Z]", line):
        # Could be metadata or a heading — let heading detection handle it,
        # but flag very short title-case lines that aren't sentences
        words = line.split()
        if len(words) <= 6 and all(w[0].isupper() for w in words if w):
            return True
    return False


def _merge_orphan_numbers(chunks: list[str]) -> list[str]:
    """Merge a lone number block ("1", "2.1") with the chunk that follows it."""
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


def _is_continuation(prev: str, curr: str) -> bool:
    """
    Return True if curr looks like a continuation of prev (same paragraph).
    Rules:
      - prev must NOT end with sentence-terminal punctuation (. ! ? : ")
        Exception: abbreviations like "Fig.", "et al.", "e.g.", "i.e." are not terminals
      - curr must NOT start like a new paragraph (capital after blank line,
        bullet, section number)
      - Neither should be a heading or standalone label
    """
    prev_last = prev.rstrip()[-1] if prev.rstrip() else ""

    # If prev ends with a real sentence terminator → new paragraph
    if prev_last in (".", "!", "?", '"', "'"):
        # But allow abbreviation endings: short word + period
        last_word = prev.rstrip().rsplit(None, 1)[-1]
        is_abbrev = (
            len(last_word) <= 5 and last_word.endswith(".")  # e.g. "Fig.", "al."
            or last_word in ("e.g.", "i.e.", "etc.", "vs.", "cf.")
        )
        if not is_abbrev:
            return False

    # If curr starts with a bullet or section-like number → new block
    if re.match(r"^[•\-\*]|^\d+\.|^[A-Z]{2,}\s", curr):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Field extractors
# ─────────────────────────────────────────────────────────────────────────────

def _find_title(blocks: list[str]) -> str:
    for b in blocks:
        if b.strip():
            return b.strip().split("\n")[0].strip()
    return "Untitled"


def _find_authors(blocks: list[str], title: str) -> list[Author]:
    """
    Detect author blocks. Handles three formats:

    Springer format — all authors on one line separated by · :
        "Haolin Yu1,2 · Kaiyang Guo3 · Mahdi Karami1 · ..."

    PDF format — each author info spread across consecutive single-line blocks:
        "Cindy Norris"
        "Department of Computer Science"
        ...

    DOCX format — each author is one multi-line paragraph:
        "Peter Szabó\\nDepartment of...\\nUniversity...\\nCity\\nemail"
        "Miroslava Ferencová\\nDepartment of...\\n..."
    """
    authors = []

    for block in blocks[1:15]:
        if block.lower() == "abstract" or _detect_heading(block):
            break
        if block == title:
            continue

        # ── Springer/Nature format: "Name1,2 · Name2 · Name3 ..." ──────────
        # Single block containing · separators between author names
        if "·" in block or "\u00b7" in block:
            raw = block.replace("\n", " ")
            parts = re.split(r"\s*[·\u00b7]\s*", raw)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # Strip trailing superscript affiliation numbers like "1,2" or "3"
                name = re.sub(r"[\d,\s]+$", "", part).strip()
                # Must look like a real name: 2+ words, letters only (+ accents)
                if (re.match(r"^[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+"
                             r"(\s+[\w\u00C0-\u024F][\w\u00C0-\u024F\-]+)+$", name)
                        and len(name) < 60 and not re.search(r"\d", name)):
                    authors.append(Author(name=name))
            if authors:
                return authors
            continue

        # ── DOCX multi-line author block ──────────────────────────────────
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
                    elif re.match(r"^\(?\d[\d\s\-().]+$", line):
                        pass  # phone
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

        # ── PDF single-line format ────────────────────────────────────────
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
                    elif re.match(r"^\(?\d[\d\s\-().]+$", aff):
                        pass
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

def _parse_body(blocks: list[str], doc: Document):
    current_heading: str | None = None
    current_body:    list[str]  = []
    next_is_abstract = False
    in_references    = False

    author_names = {a.name for a in doc.authors}

    def _flush():
        if current_heading and current_body:
            doc.sections.append(Section(
                heading = current_heading,
                body    = "\n\n".join(current_body).strip()
            ))

    for block in blocks:
        lower      = block.lower().strip()
        first_line = block.split("\n")[0].strip()

        # ── Skip title / author identity blocks ───────────────────────────────
        if first_line == doc.title or first_line in author_names:
            continue

        # ── Image marker from layout_parser ──────────────────────────────────
        if block.startswith("__IMAGE__"):
            img_path = re.search(r"__IMAGE__(.*?)__PAGE_", block)
            if img_path and current_heading is not None:
                path = img_path.group(1)
                # Store pending image — will be paired with next caption
                current_body.append(f"__PENDING_IMG__{path}")
            continue

        # ── Figure caption — pair with pending image ──────────────────────────
        if re.match(r"^(fig\.?|figure)\s*\d+", lower):
            caption = block.strip()
            # Look back for a pending image in current_body
            fig_tex = None
            for i in range(len(current_body) - 1, -1, -1):
                if current_body[i].startswith("__PENDING_IMG__"):
                    img_path = current_body[i][len("__PENDING_IMG__"):]
                    fig_tex = _build_figure(img_path, caption)
                    current_body[i] = "%%RAWTEX%%" + fig_tex + "%%ENDRAWTEX%%"
                    break
            # No pending image found — still emit caption as a figure placeholder
            if fig_tex is None and current_heading is not None:
                current_body.append(f"%%RAWTEX%%% Figure: {caption}%%ENDRAWTEX%%")
            continue

        # ── Standalone "Abstract" label ───────────────────────────────────────
        if lower == "abstract":
            next_is_abstract = True
            continue

        if next_is_abstract:
            doc.abstract = block.strip()
            next_is_abstract = False
            continue

        # ── Inline "Abstract — text..." ──────────────────────────────────────
        if re.match(r"^abstract[\s:—\-]+\S", block, re.IGNORECASE) and not doc.abstract:
            doc.abstract = re.sub(r"^abstract[\s:—\-]+", "", block, flags=re.IGNORECASE).strip()
            continue

        # ── Keywords ─────────────────────────────────────────────────────────
        if re.match(r"^(keywords?|index terms?)[\s:—\-]", lower):
            kw = re.sub(r"^(keywords?|index terms?)[\s:—\-]+", "", block, flags=re.IGNORECASE)
            doc.keywords = [k.strip() for k in re.split(r"[;,]", kw) if k.strip()]
            continue

        # ── References section ────────────────────────────────────────────────
        if re.match(r"^references\.?$", lower):
            _flush()
            current_heading = None
            current_body    = []
            in_references   = True
            continue

        if in_references:
            _accumulate_reference(block, doc)
            continue

        # ── DOCX table block ──────────────────────────────────────────────────
        if block.startswith("DOCX_TABLE_START"):
            table_tex = _build_docx_table(block)
            if table_tex and current_heading is not None:
                current_body.append("%%RAWTEX%%" + table_tex + "%%ENDRAWTEX%%")
            continue

        # ── Display math equation ($$...$$) ──────────────────────────────────
        if block.startswith("$$") and block.endswith("$$"):
            if current_heading is not None:
                # Pass through as raw LaTeX — already valid
                inner = block[2:-2].strip()
                current_body.append("%%RAWTEX%%\\begin{equation}\n" + inner + "\n\\end{equation}%%ENDRAWTEX%%")
            continue

        # ── Section / subsection heading ─────────────────────────────────────
        heading = _detect_heading(block)
        if heading:
            _flush()
            current_heading = heading
            current_body    = []
            continue

        # ── Table cluster (PDF tables) ────────────────────────────────────────
        table_tex = _try_build_table(block)
        if table_tex:
            if current_heading is not None:
                current_body.append("%%RAWTEX%%" + table_tex + "%%ENDRAWTEX%%")
            continue

        # ── Body text ────────────────────────────────────────────────────────
        if current_heading is not None:
            current_body.append(block.strip())

    _flush()


# ─────────────────────────────────────────────────────────────────────────────
# Table reconstruction
# ─────────────────────────────────────────────────────────────────────────────

def _try_build_table(block: str) -> str | None:
    """
    Reconstruct a LaTeX table from a raw extracted block.

    The block contains (all newline-separated):
      - Optionally: a trailing body-text line before the table
      - Header token lines  (text labels: "Benchmark", "8", "long", "RASSG"...)
      - Data rows           (label + numbers: "loop1", "1.56", "1.02", ...)
      - Group labels        ("Livermore", "Clinpack" — text between data rows)
      - Caption line        ("Table 1: ..." possibly split across two lines)
    """
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    if len(lines) < 5:
        return None

    # Strip leading lines that are clearly body text (end with punctuation
    # but aren't table tokens or a Table caption)
    while lines:
        l = lines[0]
        if re.match(r"^Table\s+\d+", l, re.IGNORECASE):
            break
        # Body text: ends with sentence punctuation AND is not a pure token
        if re.search(r"[.!?,;]$", l) and not re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", l):
            lines = lines[1:]
        else:
            break

    if len(lines) < 5:
        return None

    # ── Find and reconstruct caption ──────────────────────────────────────────
    caption_idx = None
    caption_text = ""
    for i, l in enumerate(lines):
        # Match "Table N:" or "Table N " (with or without colon)
        m = re.match(r"^Table\s+\d+[:.]\s*(.*)", l, re.IGNORECASE)
        if not m:
            # Also match bare "Table N" — caption text may be on next line
            m2 = re.match(r"^Table\s+\d+$", l, re.IGNORECASE)
            if m2 and i + 1 < len(lines):
                caption_idx = i
                caption_text = lines[i + 1] if i + 1 < len(lines) else ""
                break
        if m:
            caption_idx = i
            caption_text = m.group(1).strip()
            # Join hyphenated continuation
            if caption_text.endswith("-") and i + 1 < len(lines):
                caption_text = caption_text[:-1] + lines[i + 1]
            elif i + 1 < len(lines) and not re.match(r"^\d", lines[i + 1]):
                next_l = lines[i + 1]
                if len(next_l.split()) <= 6:
                    caption_text = caption_text + " " + next_l
            break

    if caption_idx is None:
        return None

    data_lines = lines[:caption_idx]

    # Must have at least some numbers
    num_count = sum(1 for l in data_lines if re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", l))
    if num_count < 3:
        return None

    # ── Determine data row width ──────────────────────────────────────────────
    # Find the first data row (text label followed by numbers) to get n_cols
    n_cols = 0
    for idx, l in enumerate(data_lines):
        if not re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", l) and idx + 1 < len(data_lines):
            if re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", data_lines[idx + 1]):
                # Count consecutive numbers after this label
                j = idx + 1
                count = 0
                while j < len(data_lines) and re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", data_lines[j]):
                    count += 1
                    j += 1
                if count > n_cols:
                    n_cols = count
    if n_cols == 0:
        return None

    # ── Split into header tokens and data section ─────────────────────────────
    # Strategy: group labels (Livermore, Clinpack) clearly mark where data starts.
    # Everything before the FIRST group label is header material.
    # A group label is a text line that is NOT followed by a number.
    first_group_idx = len(data_lines)
    for idx in range(len(data_lines)):
        l = data_lines[idx]
        if re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", l):
            continue
        next_is_num = (idx + 1 < len(data_lines) and
                       re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", data_lines[idx + 1]))
        if not next_is_num and idx > 0:  # text line not followed by number = group label
            first_group_idx = idx
            break

    header_tokens = data_lines[:first_group_idx]
    # Remove leading body-text lines
    header_tokens = [t for t in header_tokens
                     if not (re.search(r"[.!?,;]$", t) and len(t.split()) > 2)]
    header_end = first_group_idx

    # ── Parse data rows and group labels ─────────────────────────────────────
    rows   = []   # (label, [val...])
    groups = {}   # row_index → group_name

    i = header_end
    while i < len(data_lines):
        l = data_lines[i]
        if re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", l):
            i += 1
            continue

        # Is it a group label? — text line NOT followed immediately by a number
        next_is_num = (i + 1 < len(data_lines) and
                       re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", data_lines[i + 1]))
        if not next_is_num:
            groups[len(rows)] = l
            i += 1
            continue

        # Data row: collect label + following numbers
        vals = []
        i += 1
        while i < len(data_lines) and re.match(r"^\d+\.?\d*[⇑⇓↑↓\u2191\u2193\u21D1\u21D3\*]?$", data_lines[i]):
            vals.append(data_lines[i])
            i += 1
        rows.append((l, vals))

    if not rows:
        return None

    # ── Build LaTeX ───────────────────────────────────────────────────────────
    def esc(t):
        return t.replace("_", r"\_").replace("%", r"\%").replace("&", r"\&")

    col_spec = "l" + "r" * n_cols

    tex = [
        r"\begin{table}[h!]",
        r"\caption{" + esc(caption_text) + "}",
        r"\begin{center}",
        r"\resizebox{\columnwidth}{!}{",
        r"\begin{tabular}{l|" + "r" * n_cols + "}",
        r"\hline",
    ]

    # ── Header rows ───────────────────────────────────────────────────────────
    # header_tokens = [Benchmark, Number of Registers, 8, 16,
    #                  long, medium, long, medium,
    #                  RASSG, REGOA, RASSG, REGOA, RASSG, REGOA, RASSG, REGOA]
    # We know n_cols = 8 (from data rows).
    # Row label col = "Benchmark", then n_cols value columns.
    if header_tokens:
        htoks = header_tokens[:]
        row_label_hdr = htoks[0] if htoks else ""  # "Benchmark"
        rest = htoks[1:]                             # remaining header tokens

        # Row 1: "Benchmark" | spanning header (e.g. "Number of Registers")
        # Find the spanning label — first multi-word token in rest
        span_label = ""
        span_start = 0
        for k, t in enumerate(rest):
            if len(t.split()) > 1:   # multi-word = spanning header
                span_label = t
                span_start = k
                break

        if span_label:
            # Tokens before span_label are empty padding
            tex.append(
                "  \\textbf{" + esc(row_label_hdr) + "} & "
                r"\multicolumn{" + str(n_cols) + r"}{c}{\textbf{" + esc(span_label) + r"}} \\"
            )
            sub_tokens = rest[span_start + 1:]  # 8, 16, long, medium, ..., RASSG, REGOA
        else:
            tex.append(
                "  \\textbf{" + esc(row_label_hdr) + "} & " +
                " & ".join("\\textbf{" + esc(t) + "}" if t else "" for t in (rest + [""] * n_cols)[:n_cols]) +
                r" \\"
            )
            sub_tokens = []

        # Row 2+: distribute remaining sub-tokens as additional header rows
        # Each row fills n_cols value columns
        while sub_tokens:
            chunk = (sub_tokens[:n_cols] + [""] * n_cols)[:n_cols]
            sub_tokens = sub_tokens[n_cols:]
            # Only emit row if it has non-empty content
            if any(chunk):
                tex.append(
                    "  & " +
                    " & ".join("\\textbf{" + esc(t) + "}" if t else "" for t in chunk) +
                    r" \\"
                )

        tex.append(r"\hline")

    # ── Data rows ─────────────────────────────────────────────────────────────
    for idx, (label, vals) in enumerate(rows):
        if idx in groups:
            tex.append(r"\multicolumn{" + str(n_cols + 1) +
                       r"}{l}{\textbf{" + esc(groups[idx]) + r"}} \\")
        padded = (vals + [""] * n_cols)[:n_cols]
        tex.append("  " + esc(label) + " & " + " & ".join(padded) + r" \\")

    tex += [r"\hline", r"\end{tabular}", r"}", r"\end{center}", r"\end{table}"]
    return "\n".join(tex)


# ─────────────────────────────────────────────────────────────────────────────
# Reference accumulator
# ─────────────────────────────────────────────────────────────────────────────

def _accumulate_reference(block: str, doc: Document):
    """Add block content to doc.references, merging continuations."""
    parts = re.split(r"(?=\[\d+\])", block)
    for part in parts:
        part = re.sub(r"^\[\d+\]\s*", "", part).strip()
        if not part:
            continue
        # Merge into last ref if:
        # - we have a previous ref, AND
        # - previous ref does NOT end with a year like "1993." or "1992." (complete entry)
        if doc.references:
            prev = doc.references[-1]
            prev_complete = bool(re.search(r"\b(19|20)\d{2}\.$", prev.strip()))
            if not prev_complete:
                doc.references[-1] += " " + part
                continue
        doc.references.append(part)


def _split_references(block: str) -> list[str]:
    stripped = re.sub(r"^references[\s\n]*", "", block, flags=re.IGNORECASE).strip()
    if not stripped:
        return []
    parts = re.split(r"(?=\[\d+\])", stripped)
    parts = [" ".join(p.split()) for p in parts if p.strip()]
    return parts if len(parts) > 1 else [" ".join(stripped.split())]


# ─────────────────────────────────────────────────────────────────────────────
# Heading detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_heading(block: str) -> str | None:
    stripped = block.strip()
    lines = stripped.split("\n")

    # Two-line: "1\nIntroduction" or "2.1\nSubsection"
    if len(lines) == 2:
        num, text = lines[0].strip(), lines[1].strip()
        if re.match(r"^\d+(\.\d+)*\.?$|^[ivxlcdmIVXLCDM]+\.?$", num, re.IGNORECASE):
            clean = _LEADING_NUM.sub("", text).strip().rstrip(".")
            if clean.lower() in IEEE_SECTION_NAMES or (text.isupper() and len(text) < 60):
                return clean
            if len(clean) < 60 and not re.search(r"[.!?]", clean):
                return clean

    # Single line
    if len(stripped) <= 80:
        # Roman numeral section: "I. INTRODUCTION", "II. NUMBERS"
        m = re.match(r"^([IVXLCDM]+)\.\s+(.+)$", stripped)
        if m and len(stripped) < 60:
            return m.group(2).title() if m.group(2).isupper() else m.group(2)

        # Letter subsection: "A. Mathematical Formulas", "B. Physical Formulas"
        m = re.match(r"^([A-Z])\.\s+(.+)$", stripped)
        if m and len(stripped) < 80 and not re.search(r"[.!?]$", stripped):
            return m.group(2)

        clean = _LEADING_NUM.sub("", stripped).strip().rstrip(".")
        if clean.lower() in IEEE_SECTION_NAMES:
            return clean
        if stripped.isupper() and 3 < len(stripped) < 60:
            return stripped.title()

    return None


def _build_docx_table(block: str) -> str | None:
    """
    Reconstruct a LaTeX table from a DOCX_TABLE_START...DOCX_TABLE_END block.
    Rows are pipe/ampersand-delimited.
    """
    inner = re.sub(r"^DOCX_TABLE_START\n?", "", block)
    inner = re.sub(r"\nDOCX_TABLE_END$", "", inner)
    lines = [l.strip() for l in inner.split("\n") if l.strip()]

    if not lines:
        return None

    # Find caption line
    caption = ""
    cap_idx = None
    for i, l in enumerate(lines):
        m = re.match(r"^Table\s+\d+[:.]\s*(.*)", l, re.IGNORECASE)
        if m:
            caption = m.group(1).strip() or l
            cap_idx = i
            break
    if cap_idx is not None:
        lines = lines[:cap_idx]

    def esc(t):
        """Escape a table cell. If it contains math chars, wrap in $...$."""
        t = t.strip()
        # Already looks like inline math: (a^2+b^2=c^2) or $...$
        if re.match(r"^\(.*\)$", t) and re.search(r"[\^_\\]", t):
            # Strip outer parens and wrap in $...$
            inner = t[1:-1]
            return "$" + inner + "$"
        if t.startswith("$") and t.endswith("$"):
            return t  # already math
        # Plain text — escape special chars
        t = t.replace("\\", r"\textbackslash{}")
        t = t.replace("_", r"\_")
        t = t.replace("%", r"\%")
        t = t.replace("&", r"\&")
        t = t.replace("#", r"\#")
        t = t.replace("~", r"\textasciitilde{}")
        # ^ outside math crashes — replace with text
        t = t.replace("^", r"\textasciicircum{}")
        return t

    # Split rows — cells separated by " & " or " | "
    rows = []
    for l in lines:
        if " & " in l:
            cells = [c.strip() for c in l.split(" & ")]
        elif " | " in l:
            cells = [c.strip() for c in l.split(" | ")]
        else:
            cells = [l.strip()]
        rows.append(cells)

    if not rows:
        return None

    n_cols = max(len(r) for r in rows)
    col_spec = "|" + "|".join(["l"] * n_cols) + "|"

    tex = [
        r"\begin{table}[h!]",
        r"\caption{" + esc(caption if caption else "Table") + "}",
        r"\begin{center}",
        r"\resizebox{\columnwidth}{!}{",
        r"\begin{tabular}{" + col_spec + "}",
        r"\hline",
    ]

    for i, row in enumerate(rows):
        padded = (row + [""] * n_cols)[:n_cols]
        escaped = [esc(c) for c in padded]
        if i == 0:
            # Header row — bold
            escaped = ["\\textbf{" + c + "}" if c else "" for c in escaped]
        tex.append("  " + " & ".join(escaped) + r" \\")
        if i == 0:
            tex.append(r"\hline")

    tex += [r"\hline", r"\end{tabular}", r"}", r"\end{center}", r"\end{table}"]
    return "\n".join(tex)


def _build_figure(img_path: str, caption: str) -> str:
    """
    Build a LaTeX figure environment for an extracted image.
    img_path is relative to project root — converted to absolute for pdflatex.
    """
    import os as _os
    from core.config import ROOT
    from stages.normalizer import _fix_math_symbols, _fix_unicode_spaces, _strip_control_chars

    def esc(t):
        # 1. Strip control chars and normalise unicode spaces
        t = _strip_control_chars(t)
        t = _fix_unicode_spaces(t)
        # 2. Replace Unicode math symbols with LaTeX equivalents
        t = _fix_math_symbols(t)
        # 3. Collapse any newlines — \caption{} cannot span paragraphs
        t = re.sub(r"\s*\n\s*", " ", t).strip()
        # 4. Escape remaining LaTeX special chars (not already wrapped in $...$)
        import re as _re
        parts = _re.split(r"(\$[^$]*\$)", t)
        result = []
        for part in parts:
            if part.startswith("$") and part.endswith("$"):
                result.append(part)  # math — pass through
            else:
                result.append(
                    part.replace("_", r"\_")
                        .replace("%", r"\%")
                        .replace("&", r"\&")
                        .replace("#", r"\#")
                )
        return "".join(result)

    abs_path = _os.path.join(ROOT, img_path).replace("\\", "/")

    num_match = re.search(r"(\d+)", caption)
    label = f"fig{num_match.group(1)}" if num_match else "fig"

    return (
        r"\begin{figure}[h!]" + "\n"
        r"\centering" + "\n"
        r"\includegraphics[width=\columnwidth]{" + abs_path + "}\n"
        r"\caption{" + esc(caption) + "}\n"
        r"\label{" + label + "}\n"
        r"\end{figure}"
    )