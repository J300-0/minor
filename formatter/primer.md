# AI Paper Formatter — Developer Primer

> Quick reference for understanding, running, and debugging the pipeline.
> Read this when something breaks or before adding a new feature.

---

## What it does

Takes a research paper (PDF or DOCX) and reformats it into a LaTeX PDF
matching a target academic template — IEEE, ACM, Springer, Elsevier, APA, arXiv.

```
python main.py --input input/paper.pdf --template ieee
# → output/generated_ieee.pdf
```

---

## Pipeline at a glance

```
1. Extract   →  2. Parse   →  3. Normalize  →  2.5. Canon  →  4. Render  →  5. Compile
PDF/DOCX         raw text       clean unicode      validate +     Jinja2 →      pdflatex
→ raw text    →  → Document  →  math symbols  →  repair doc  →  .tex file  →  .pdf file
```

The stages are numbered to match what appears in the log. Canon (2.5) sits between
Parse and Render as a safety gate — bad documents never reach the renderer.

---

## Running

```bash
# Basic
python main.py input/paper.pdf --template ieee

# Other templates
python main.py input/paper.pdf --template acm
python main.py input/paper.pdf --template springer
python main.py input/paper.pdf --template elsevier
python main.py input/paper.pdf --template apa
python main.py input/paper.pdf --template arxiv

# Check logs after every run
cat logs/pipeline_latest.log
```

---

## Stage 1 — Extractor (`extractor/`)

**What it does**: reads the PDF or DOCX and returns a dict with:
- `blocks` — list of `{text, font_size, bold, page}` dicts
- `tables` — list of raw table objects from pdfplumber
- `images` — list of `{path, page, bbox}` dicts

**Key rule**: PyMuPDF (`fitz`) is the primary PDF extractor.
pdfplumber is used ONLY for table detection, never for body text.
Springer PDFs use CID/Adobe-Identity-UCS encoding that causes pdfplumber
to silently return garbled text or nothing. PyMuPDF handles it correctly.

**When no font info is available** (pdfplumber fallback): all blocks get
`font_size=10, bold=False`, which tells the parser to use text-only mode.

**Images**: extracted via pdfplumber page cropping (PyMuPDF image API not
used due to proxy restrictions). Saved to `intermediate/images/`.

---

## Stage 2 — Parser (`parser/heuristic.py`)

**What it does**: converts the raw block list into a `Document` dataclass
with `title`, `authors`, `abstract`, `keywords`, `sections`, `references`.

**Two modes** (selected automatically):
- **Font-aware** (Mode A): when blocks have real font_size/bold data.
  Uses font size threshold (median + 1.5pt) to detect headings.
- **Text-only** (Mode B): when all blocks have uniform font metadata.
  Uses `_HEADING_RE` regex (numbered headings like "2.1 Related Work")
  and `_SECTION_NAMES` set to detect section boundaries.

**Critical state machine** (text-only mode):
```
title scan (first 30 lines)
  → author extraction (until "Abstract" OR numbered heading OR line 90)
    → abstract collection (until next heading)
      → keyword extraction
        → section body loop
          → reference extraction (after "References")
```

The author loop has a hard stop at `abstract_limit + 60` lines and breaks
immediately on a numbered heading — this prevents appendix titles and
experiment section headings from being extracted as author names.

**Metadata filtering** (`_is_metadata_line`): drops journal page headers,
DOI lines, copyright notices, Springer narrow no-break space markers
(U+202F), page number patterns, and editor/affiliation footnotes.

**Section skip logic** (`skip_until_heading` flag): when a Table/Figure
caption line is encountered in a section body, the state machine enters
skip mode and discards all lines until the next numbered section heading.
This prevents table data from polluting body text.

**Known limitation**: The section heading regex `_HEADING_RE` requires
headings to start with a digit or roman numeral. Unnumbered headings
(e.g., "Conclusion" alone on a line) are detected via `_SECTION_NAMES`
match. Free-form headings in unusual fonts may not be detected.

---

## Stage 3 — Normalizer (`normalizer/cleaner.py`)

**What it does**: pure text transforms on the Document — no external calls.

**Five-step `_clean_with_math()` pipeline** (used for abstract and bodies):
```
1. _clean()                  — ligatures, unicode spaces, math symbols → $\cmd$
2. _merge_adjacent_math()    — $\alpha$$\beta$ → $\alpha\beta$
3. _fix_superscript_space()  — "$\sigma$ 2" → "$\sigma^{2}$"
4. _consolidate_math_lines() — if >60% math content, wrap whole line in $...$
5. _compact_equation_lines() — remove pdfplumber spaces in lines ending with (N)
```

**`_clean()` — character-level**:
- Replaces ligatures (fi, fl, ff, ffi, ffl) with ASCII equivalents
- Maps unicode spaces to regular space
- Strips control characters and PUA chars
- Replaces math symbols (Greek, operators, arrows, set theory) with `$\cmd$`
- Safety net: NFKD decomposition for unknown chars; outputs `?` if no ASCII approx
  (check `logs/pipeline_latest.log` for `Safety net: unknown U+XXXX` debug lines)

**Common `?` causes** — characters not yet in `_MATH`:
If you see `?` in the output, search the log for `Safety net: unknown U+` to get
the codepoint. Then add it to the `_MATH` dict in `cleaner.py`:
```python
# Example: adding U+2250 (≐, approaches the limit)
"\u2250": r"\doteq",
```

**`_fix_superscript_space()`**: pdfplumber extracts superscript digits
on the same baseline as their base with a space: `σ 2` instead of `σ²`.
This function converts `$\sigma$ 2` → `$\sigma^{2}$` post-merge.
Handles negative exponents too: `$\sigma$ -1` → `$\sigma^{-1}$`.

**`_compact_equation_lines()`**: equation-numbered lines (ending with `(N)`)
have intra-formula spaces compacted — spaces after `(` and before `)` are
removed, and spaces between a word char and `(` are removed.
Only equation-numbered lines are touched; prose is never modified.

**Formula display math**: we deliberately use inline `$...$` not display
`\[...\]` math. Display math requires exact paragraph boundary placement
in the `.tex` file; Jinja2 block templating makes that unreliable and
causes fatal "Display math should end with $$" pdflatex errors.

---

## Stage 2.5 — Canon (`canon/`)

**What it does**: validates and repairs every field of the Document,
assigns a confidence score, and blocks rendering if the document is too bad.

```python
from canon.builder import build_canonical

canon_doc = build_canonical(doc)
print(canon_doc.title.confidence)   # 0.0–1.0
print(canon_doc.repair_log)         # list of repairs made
if not canon_doc.is_renderable():
    raise RuntimeError("Document not renderable")

doc = canon_doc.to_document()       # unwrap back to plain Document
```

**Repair chains** — each field has a fallback sequence:

| Field      | Primary         | Fallback 1                | Default          |
|------------|-----------------|---------------------------|------------------|
| title      | parsed title    | first section heading     | "Untitled Paper" |
| abstract   | parsed abstract | "Abstract" section body   | ""               |
| keywords   | parsed keywords | regex from abstract text  | []               |
| sections   | parsed sections | (junk sections dropped)   | []               |
| references | parsed refs     | (boilerplate dropped)     | []               |

**`is_renderable()` blocks** when:
- title is empty
- no section has a non-empty body

**Confidence scores**:
- `0.9+` — parsed cleanly
- `0.5–0.9` — repaired, usable
- `0.0–0.5` — used fallback, check logs
- `0.0` — used default placeholder

**ML path** (optional): `canon/classifier.py` provides a sklearn LinearSVC
for line-level classification. Not active unless `line_classifier.pkl` exists.
Run `python -m canon.classifier label/train/predict` to build it.

---

## Stage 4 — Renderer (`renderer/jinja_renderer.py`)

**What it does**: fills the Jinja2 template with Document fields → `.tex` file.

**Non-standard Jinja2 delimiters** (avoid conflict with LaTeX `{}` and `%`):
```
\VAR{expr}    — variable output   (instead of {{ }})
\BLOCK{stmt}  — block statement   (instead of {% %})
%%            — comment           (instead of {# #})
```

**Custom filters** available in templates:
- `latex_escape` — escapes `& % $ # _ { } ~ ^ \` for safe text insertion
- `latex_paragraphs` — converts `\n\n` to `\n\n` (paragraph break in LaTeX)
- `render_table` — renders a `Table` object as a `tabular` environment

**Figures**: image paths are copied relative to the `.tex` file and rendered
with `\includegraphics`. Only images that exist on disk are included.

**Template isolation**: IEEE/ACM/Springer/Elsevier each have their own
`template.tex.j2` and `.cls` file. Never hardcode template-specific
logic in shared code — put it in the template file.

---

## Stage 5 — Compiler (`compiler/latex_compiler.py`)

**What it does**: runs `pdflatex` twice (for cross-references) on the `.tex`
file and moves the resulting PDF to `output/generated_{template}.pdf`.

**Flags**: `-interaction=nonstopmode -halt-on-error`

**Two-pass reason**: the first pass writes `.aux` with section numbers and
citation keys; the second pass reads them to resolve `\ref{}` and `\cite{}`.

**If compilation fails**: check `intermediate/generated.log` (the pdflatex
transcript) — it contains the exact error line and context.

---

## Data model (`core/models.py`)

```python
@dataclass
class Document:
    title:      str
    authors:    List[Author]
    abstract:   str
    keywords:   List[str]
    sections:   List[Section]
    references: List[Reference]

@dataclass
class Author:
    name, department, organization, city, country, email: str

@dataclass
class Section:
    heading: str
    body:    str          # plain text — normalizer adds $...$ inline math
    depth:   int          # 1=section, 2=subsection, 3=subsubsection
    tables:  List[Table]
    figures: List[Figure]

@dataclass
class Table:
    caption: str
    headers: List[str]
    rows:    List[List[str]]
    notes:   str

@dataclass
class Figure:
    caption:    str
    image_path: str       # relative path, filled by renderer
    label:      str
```

---

## Debugging guide

### "sections=54" or wrong author names
The author extraction loop ran past the author zone and grabbed section
headings as author names. Check:
1. Was `abstract_limit + 60` hit? (look for many authors in the log)
2. Did the title scan find "Abstract" before line 30?
3. Are there numbered section headings above the abstract in the PDF?

The loop now breaks on numbered headings — but the regex `_HEADING_RE`
requires `"N Title"` or `"II. Title"` format. Unnumbered templates
(e.g., Springer LNCS) may not trigger this break.

### "title='1 3'" or similar short garbled title
Springer journals print narrow no-break space (U+202F) in page markers
like `"1 3"` (meaning page 1, column 3). This is now filtered by
`_is_metadata_line()` and penalized (-5) in title scoring.

If you see another garbage title, add the pattern to `_is_metadata_line`.

### "?" characters in the PDF output
A math symbol wasn't in `_MATH` and had no ASCII NFKD approximation.
1. Open `logs/pipeline_latest.log`
2. Search for `Safety net: unknown U+`
3. Look up the codepoint (e.g., U+2250 = `≐`)
4. Add it to `_MATH` in `normalizer/cleaner.py`

### Formula spacing — "f ( · ) ∼ GP ( 0 , k ( · , · ))"
pdfplumber inserts spaces between individual characters in formula regions.
For equation-numbered lines (ending with `(N)`), `_compact_equation_lines()`
removes the paren-adjacent spaces automatically. For non-numbered formula
lines, the spaces remain — this is a fundamental pdfplumber limitation
without position/font data. Possible workaround: switch to PyMuPDF extraction.

### "σ 2 I" instead of "σ²I"
pdfplumber places superscript digits on the same baseline with a space.
`_fix_superscript_space()` in the normalizer handles `$\sigma$ 2` →
`$\sigma^{2}$` post-merge. If you still see this, the line may have been
consolidated to a single `$...$` block already, in which case LaTeX renders
`\sigma 2` as "σ 2" (two separate tokens). File a known issue.

### "Page N of M" appearing in section bodies
`_is_metadata_line()` filters this pattern. If it reappears:
1. Check the exact format in `intermediate/structured.json`
2. The pattern `Page\s+\d+\s+of\s+\d+` (case-insensitive) should catch it
3. Some journals use `p. N of M` or `(N/M)` — add a new regex variant

### Table or figure caption leaking into body text
The `skip_until_heading` state flag in `_parse_text_only` should block
caption + data. If a specific caption pattern isn't caught:
```python
# In the section body loop in _parse_text_only:
if re.match(r"^Table\s+\d+\b", line):
    skip_until_heading = True
    continue
if re.match(r"^(?:Fig\.?|Figure)\s+\d+\b", line, re.IGNORECASE):
    skip_until_heading = True
    continue
```
The skip ends only at the next numbered heading (`_HEADING_RE` match).

### "Overfull hbox" warnings in pdflatex log
Wide tables overflow the column width. The renderer wraps tables in
`\resizebox{\columnwidth}{!}{...}` but this fails for tables with >5 columns
containing long cell text. Workaround: reduce font size in the table template
or break the table into two tables manually.

---

## File locations

```
formatter/
  main.py                    ← CLI entry point (start here)
  requirements.txt
  CLAUDE.md                  ← session memory (updated each session)
  primer.md                  ← this file
  core/
    config.py                ← template registry, path constants
    models.py                ← Document, Author, Section, Table, Figure, Reference
    pipeline.py              ← orchestrator (calls each stage in order)
    logger.py                ← rotating log + latest log symlink
  extractor/
    pdf_extractor.py         ← PyMuPDF primary; pdfplumber tables+images only
    docx_extractor.py        ← python-docx
  parser/
    heuristic.py             ← Mode A (font-aware) + Mode B (text-only)
  normalizer/
    cleaner.py               ← unicode, ligatures, math → LaTeX inline math
  canon/
    models.py                ← CanonicalDocument, FieldResult
    builder.py               ← validate + repair + score
    features.py              ← 16 line features for scoring (ML foundation)
    classifier.py            ← optional sklearn LinearSVC
  renderer/
    jinja_renderer.py        ← Jinja2 → .tex
  compiler/
    latex_compiler.py        ← pdflatex 2-pass
  template/
    ieee/     (IEEEtran.cls + template.tex.j2)
    acm/      (acmart-tagged.cls + template.tex.j2)
    springer/ (llncs.cls + template.tex.j2)
    elsevier/ (elsarticle.cls + template.tex.j2)
    apa/      (template.tex.j2)
    arxiv/    (template.tex.j2)
  input/                     ← drop PDFs/DOCX here
  intermediate/              ← extracted.txt, structured.json, generated.tex
  output/                    ← final PDFs
  logs/                      ← pipeline.log + pipeline_latest.log
```

---

## Invariants — never break these

1. **PyMuPDF is primary. pdfplumber is tables+images only.**
   pdfplumber silently corrupts CID-encoded Springer PDFs.

2. **Canon gate is mandatory.**
   Never pass a `Document` directly to the renderer. Always go through
   `build_canonical()` first. The renderer expects non-None, clean fields.

3. **Silence pdfminer's DEBUG flood.**
   ```python
   logging.getLogger("pdfminer").setLevel(logging.WARNING)
   ```

4. **Inline math only (`$...$`). Never display math (`\[...\]`).**
   Display math needs exact paragraph boundaries. Jinja2 templating can't
   guarantee them reliably → fatal pdflatex errors.

5. **Template isolation.**
   No IEEE-specific code in shared modules. Each template's `.j2` file
   handles its own structure.

6. **One change at a time.**
   The March 2025 breakage happened because LLM integration and
   multi-template support were added in the same commit.
