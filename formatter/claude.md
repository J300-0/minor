# CLAUDE.md — ai-paper-formatter Project Memory
> Read this file at the start of every session. Update it when structural changes are made.
> Last updated: 2026-03-21 — Subprocess OCR (pix2tex/nougat/Tesseract), pipeline reorder

---

## What This Project Does

Takes a research paper (PDF or DOCX) — often messy, semi-structured — and reformats
it into a clean LaTeX PDF matching a target academic template (IEEE, ACM, Springer, etc.).

```
Input: any_paper.pdf + --template ieee
Output: generated_ieee.pdf  (properly formatted LaTeX PDF)
```

**Run it:**
```bash
python main.py --input input/your_paper.pdf --template ieee
```

---

## Pipeline Stages (current architecture)

```
1. Extract   →  2. Parse   →  3. Canon    →  4. Normalize  →  5. Render  →  6. Compile
PDF/DOCX         raw text       validate +     fix unicode      Jinja2 →      pdflatex
→ raw text    →  → Document  →  repair doc →  math symbols  →  .tex file  →  .pdf file
```

### Stage 1: Extract (`extractor/`)
- `pdf_extractor.py` — **PyMuPDF (fitz) is primary**. pdfplumber for tables only.
- `docx_extractor.py` — python-docx
- `pix2tex_worker.py` — Subprocess worker for pix2tex OCR (PRIMARY equation OCR)
- `nougat_worker.py` — Subprocess worker for nougat OCR (FALLBACK)
- **Critical**: PyMuPDF handles CID/Adobe-Identity-UCS encoded PDFs (Springer). pdfplumber silently crashes on these → only use it for tables.
- **Critical**: ALL ML-based OCR (pix2tex, nougat) runs in **subprocesses** — never in the main pipeline process. This prevents segfaults/crashes from killing the pipeline.

### Stage 2: Parse (`parser/`)
- `heuristic.py` — font-aware + text-only heading detection
- Detects: title, authors, abstract, keywords, sections, references
- Uses `_is_metadata_line()` to filter page headers, DOIs, copyright lines
- Uses `_is_heading_line()` to detect section boundaries

### Stage 3: Canon (`canon/`)
- Canonical Structure Builder — validation + repair gate
- See full section below.

### Stage 4: Normalize (`normalizer/`)
- `cleaner.py` — pure local transforms, no external calls
- Fixes: ligatures (fi/fl/ff), unicode quotes/dashes, math symbols → LaTeX commands
- Greek letters, math operators, arrows, sub/superscripts
- Inline math patterns: derivatives, integrals, fractions, superscripts/subscripts

### Stage 5: Render (`renderer/`)
- `jinja_renderer.py` — Jinja2 with LaTeX-safe delimiters (`\VAR{}`, `\BLOCK{}`)
- Custom filters: `latex_escape`, `latex_paragraphs`, `render_table`
- Copies required .cls files alongside .tex
- **Only receives documents that passed canon check**

### Stage 6: Compile (`compiler/`)
- `latex_compiler.py` — pdflatex 2-pass (for cross-references)
- Flags: `-interaction=nonstopmode -halt-on-error`
- Outputs `generated_{template}.pdf`

---

## Equation OCR — Subprocess Isolation Pattern

### Why subprocesses?
pix2tex's `LatexOCR()` init can cause OS-level segfaults (CUDA DLL issues on Windows).
A segfault kills the entire Python process — no `try/except` can catch it.
Solution: all ML model code runs in child processes via `subprocess.run()`.

### OCR fallback chain
```
pix2tex (primary) → nougat (fallback) → Tesseract (last resort)
```

### Worker files
- `extractor/pix2tex_worker.py` — `python pix2tex_worker.py <image_path>` → prints LaTeX to stdout
- `extractor/nougat_worker.py` — `python nougat_worker.py <image_path>` → prints LaTeX to stdout
  - Pads small equation crops to 896×1152 (nougat's expected page dimensions)
  - Uses `nougat-0.1.0-small` model variant

### Availability checks
Both `_pix2tex_available()` and `_nougat_available()` test via subprocess import.
Results are cached for the process lifetime.

### OCR LaTeX sanitization (`_sanitize_ocr_latex`)
pix2tex/nougat output often has LaTeX errors that crash pdflatex. Sanitization chain:
1. `_fix_array_col_spec()` — fixes `\begin{array}{col}` when declared cols ≠ actual `&` count
2. `_fix_unbalanced_braces()` — depth-walk to balance `{`/`}`
3. `_fix_unbalanced_delimiters()` — balances `\left`/`\right` pairs
4. `_is_latex_safe()` — final gate; rejects OCR if still broken (returns empty string)

### Table cell OCR (`_ocr_cell`)
For image-based table cells: crop with 2pt border inset → render at 300 DPI → add 20% white padding → OCR via fallback chain.

---

## Stage 3 — Canonical Structure Builder

### Why it exists
Templates were breaking silently because the parser could produce:
- `None` in title/abstract fields
- Empty section bodies
- Sections with headings that were actually metadata
- Abstracts that swallowed the first body section
- References with publisher boilerplate mixed in

The canonical builder sits between Parse and Render as a **validation + repair gate**.

### Files
```
canon/
  __init__.py       # exports build_canonical
  models.py         # CanonicalDocument, FieldResult (value + confidence + source)
  builder.py        # _CanonicalBuilder class — validate, repair, score, cross-validate
  features.py       # 16-feature vector per line (foundation for ML)
  classifier.py     # OPTIONAL sklearn ML classifier (see "ML Classifier" section)
```

### How it works
```python
from canon.builder import build_canonical

canon_doc = build_canonical(doc)   # takes Document, returns CanonicalDocument

# Every field has: value, confidence (0-1), source ("parsed"|"repaired:..."|"default")
print(canon_doc.title.confidence)   # e.g. 0.9
print(canon_doc.repair_log)         # list of all repairs made

# Gate: only render if document is good enough
if not canon_doc.is_renderable():
    raise RuntimeError("Document not renderable")

doc = canon_doc.to_document()   # unwrap back to plain Document for Jinja2
```

### Repair chains (per field)
Each field has a fallback chain. If primary parse fails, next fallback is tried:

| Field      | Primary            | Fallback 1                  | Fallback 2          | Default         |
|------------|--------------------|-----------------------------|---------------------|-----------------|
| title      | parsed title       | first section heading        | body scan           | "Untitled Paper"|
| authors    | parsed authors     | —                           | —                   | []              |
| abstract   | parsed abstract    | "Abstract" section in list   | first body para     | ""              |
| keywords   | parsed keywords    | regex from abstract text     | —                   | []              |
| sections   | parsed sections    | (junk sections dropped)      | —                   | []              |
| references | parsed refs        | (boilerplate dropped)        | —                   | []              |

### Confidence scores
- `0.9+` = high confidence, parsed cleanly
- `0.5-0.9` = medium, some issues but usable
- `0.0-0.5` = low, repaired from fallback — check logs
- `0.0` = used default placeholder — extraction failed for this field

### `is_renderable()` check
Returns `True` only if:
1. title exists (confidence > 0)
2. at least 1 section with non-empty body
3. No critical field is completely missing

If `False`, pipeline raises `RuntimeError` and logs `canon_doc.summary()` — check `logs/pipeline_latest.log`.

### ML Classifier (optional, future)
`canon/classifier.py` provides a path to replace/augment heuristic parsing with a trained `sklearn LinearSVC`.

**To build the training data:**
```bash
# Step 1: Auto-label lines from your extracted text
python -m canon.classifier label --input intermediate/extracted.txt --output labels.csv

# Step 2: Open labels.csv, correct the 'corrected_label' column
# Labels: heading | title | author | abstract | body | reference | metadata

# Step 3: Train
python -m canon.classifier train --labels labels.csv --output canon/line_classifier.pkl

# Step 4: Test a line
python -m canon.classifier predict --line "2. Related Work"
```

**You need ~200-300 labeled lines from 5+ different papers for reliable results.**
The model is automatically used by `LineClassifier()` once `line_classifier.pkl` exists.

---

## File Structure

```
formatter/
  main.py                    # CLI entry point
  requirements.txt           # pymupdf, pdfplumber, python-docx, jinja2, requests
  CLAUDE.md                  # THIS FILE — read first every session
  core/
    config.py                # Paths, constants, template registry
    models.py                # Dataclasses: Document, Author, Section, Table, Figure, Reference
    pipeline.py              # Orchestrator — 6 stages + canon gate
    logger.py                # Rotating + latest log
  extractor/
    pdf_extractor.py         # PyMuPDF primary, pdfplumber tables only, OCR orchestration
    pix2tex_worker.py        # Subprocess worker — pix2tex LaTeX OCR (primary)
    nougat_worker.py         # Subprocess worker — nougat OCR (fallback)
    docx_extractor.py        # python-docx
  parser/
    heuristic.py             # Font-aware + text-only heading/section detection
  normalizer/
    cleaner.py               # Unicode cleanup, ligatures, math → LaTeX
  canon/                     # Stage 3 — validation + repair gate
    __init__.py
    models.py                # CanonicalDocument + FieldResult
    builder.py               # Validate + repair + score
    features.py              # 16 line features (ML foundation)
    classifier.py            # Optional sklearn ML classifier
  renderer/
    jinja_renderer.py        # Jinja2 → LaTeX
  compiler/
    latex_compiler.py        # pdflatex 2-pass
  template/
    ieee/     (IEEEtran.cls + template.tex.j2)
    acm/      (acmart-tagged.cls + template.tex.j2)
    springer/ (llncs.cls + template.tex.j2)
    elsevier/ (elsarticle.cls + template.tex.j2)
    apa/      (template.tex.j2)
    arxiv/    (template.tex.j2)
  input/                     # Drop input PDFs/DOCX here
  intermediate/              # extracted.txt, structured.json, generated.tex
  output/                    # Final PDFs
  logs/                      # pipeline.log (rotating), pipeline_latest.log
```

---

## Templates

| Template  | .cls file         | Notes                    |
|-----------|-------------------|--------------------------|
| ieee      | IEEEtran.cls      | Conference format        |
| acm       | acmart-tagged.cls | ACM format               |
| springer  | llncs.cls         | LNCS format              |
| elsevier  | elsarticle.cls    | Elsevier journal format  |
| apa       | (none)            | APA style                |
| arxiv     | (none)            | arXiv preprint format    |

---

## Key Technical Rules (never violate these)

1. **PyMuPDF is primary extractor.** pdfplumber is ONLY for table detection.
   Springer PDFs use CID encoding that causes pdfplumber to silently fail.

2. **Canon gate is mandatory.** Never call the renderer on a Document that hasn't
   passed `build_canonical()`. The renderer expects clean, non-None fields.

3. **pdfminer DEBUG logging** floods root logger — must be explicitly silenced:
   ```python
   logging.getLogger("pdfminer").setLevel(logging.WARNING)
   ```

4. **LM Studio streaming** (if re-added) requires `timeout=(60, None)` to avoid
   per-read socket timeouts on long generations.

5. **Template isolation**: never hardcode IEEE-specific logic in shared code.
   Each template's Jinja2 template file handles its own structure.

6. **Introduce one change at a time.** The 2025-03 breakage happened because LLM
   integration and multi-template support were added simultaneously.

7. **ML OCR models MUST run in subprocesses.** pix2tex and nougat can segfault
   (CUDA DLL issues on Windows). Segfaults kill the process — no Python exception
   handler can catch them. Use `pix2tex_worker.py` / `nougat_worker.py` via
   `subprocess.run()`. Never import pix2tex or nougat in the main pipeline process.

8. **OCR output MUST be sanitized before reaching pdflatex.** Always pass through
   `_sanitize_ocr_latex()` (array col fix → brace fix → delimiter fix → safety gate).
   Reject output that fails `_is_latex_safe()` — better to skip an equation than crash.

---

## Dependencies

```
pymupdf>=1.23.0      # import as fitz — PDF extraction
pdfplumber>=0.10.0   # tables only
python-docx>=1.1.0   # DOCX extraction
jinja2>=3.1.0        # template rendering
requests>=2.31.0     # optional, future API use
pix2tex              # optional, LaTeX OCR for equation images (PRIMARY)
nougat-ocr           # optional, Meta's Nougat OCR (FALLBACK after pix2tex)
pytesseract          # optional, Tesseract OCR fallback (needs tesseract binary)
scikit-learn         # optional, for ML classifier (canon/classifier.py)
```
**System:** `pdflatex` (TeX Live / MiKTeX) must be on PATH.

### Equation OCR setup
```bash
pip install pix2tex          # primary — outputs LaTeX directly
pip install nougat-ocr       # fallback — Meta's Nougat, good for full-page equations
# OR
pip install pytesseract      # last resort — outputs plain text
sudo apt install tesseract-ocr  # needed for pytesseract
```
OCR fallback chain: pix2tex → nougat → Tesseract. All run in subprocesses.
pix2tex is preferred — outputs LaTeX like `\frac{df}{dx}` directly.
nougat is designed for full scientific pages; small crops are padded to page size.
First run of each downloads models (~300 MB for pix2tex, ~350 MB for nougat).
## CLAUDE.md UPDATE — 2026-03-22: pix2tex math_extractor integration

Paste this section into claude.md under "Changelog" and update the File Structure table.

─────────────────────────────────────────────────────────────────────
CHANGELOG ENTRY (add to top of changelog section):
─────────────────────────────────────────────────────────────────────

### 2026-03-22 — pix2tex Math Region Extractor

**Problem diagnosed**: 6 structural flaws prevented pix2tex from working:
  1. Math regions never isolated as images (image extractor only gets raster figures)
  2. No math-region detector in the codebase
  3. PyMuPDF extracts formula characters as text, not images — normalizer then garbles complex formulas
  4. Normalizer produces fragile adjacent `$...$` fragments instead of clean LaTeX
  5. No pipeline hook where pix2tex could be called
  6. No `FormulaBlock` model to carry pix2tex results through to the renderer

**Fix**:
- Added `FormulaBlock` dataclass to `core/models.py`
- Added `formula_blocks: List[FormulaBlock]` field to `Document`
- Created `extractor/math_extractor.py` — new module that:
    - Uses PyMuPDF block analysis to detect formula bounding boxes
      (by math-Unicode char count, equation-number pattern, or math font name)
    - Renders each region as a PNG via `page.get_pixmap(clip=bbox)` at 200 DPI
    - Runs pix2tex on each crop
    - Filters out results below confidence 0.45
    - Returns list of dicts, one per formula
- `pdf_extractor.py`: call `extract_formula_blocks()` at end of Stage 1, add to return dict
- `pipeline.py`: attach `formula_blocks` to `Document` after `_parse()`
- `renderer/jinja_renderer.py`: pass `formula_blocks` to template context
- Templates: render formulas as `\begin{equation}...\end{equation}` blocks

**Graceful degradation**: if `pix2tex` not installed → `[]` returned, pipeline unchanged.
Install: `pip install pix2tex`

─────────────────────────────────────────────────────────────────────
FILE STRUCTURE UPDATE (update the tree in claude.md):
─────────────────────────────────────────────────────────────────────

Under extractor/, add:
    math_extractor.py    # NEW: detect formula regions → crop → pix2tex → FormulaBlock list

Under core/models.py description, update to:
    models.py            # Dataclasses: Document, Author, Section, Table, Figure,
                         #              FormulaBlock (NEW), Reference

─────────────────────────────────────────────────────────────────────
KNOWN ISSUES UPDATE (update the todo list in claude.md):
─────────────────────────────────────────────────────────────────────

Add to HIGH priority:
- [ ] Test pix2tex on FedBNR paper — verify confidence scores + formula quality
- [ ] Embed formula blocks at source location (by page proximity to sections)
      Currently all formulas go to a Key Equations block at end of document

─────────────────────────────────────────────────────────────────────
KEY TECHNICAL RULES (add rule 4 to the rules section in claude.md):
─────────────────────────────────────────────────────────────────────

4. **pix2tex confidence filter**: always discard FormulaBlocks with confidence < 0.45.
   Bad OCR output inserted verbatim into LaTeX is worse than no formula at all.
---

## Known Issues (as of 2026-03-21)

1. **Overfull hbox** — wide tables overflow column width despite `\resizebox` for >5 columns
2. **ACM .cls file** — needs verification that acmart-tagged.cls is correct version
3. **Multi-column extraction** — hyphenation artifacts from column layout in double-column PDFs
4. **Reference cleaning** — incomplete; some entries still contain in-text citation noise

---

## Changelog

### 2026-03-21 — Subprocess OCR isolation, nougat fallback, LaTeX sanitization, pipeline reorder
- **Subprocess isolation**: ALL ML OCR (pix2tex, nougat) now runs in child processes
  - `pix2tex_worker.py`: standalone subprocess worker for pix2tex
  - `nougat_worker.py`: standalone subprocess worker for Meta's Nougat
  - Prevents segfaults (CUDA DLL issues) from killing the main pipeline
  - `_run_worker()` common subprocess helper with configurable timeout
- **OCR fallback chain**: pix2tex → nougat → Tesseract (via `_ocr_equation()`)
  - `_pix2tex_available()`: 2-stage subprocess check (import then full init)
  - `_nougat_available()`: subprocess import check
- **LaTeX sanitization** (`_sanitize_ocr_latex()`): 4-step chain
  - `_fix_array_col_spec()`: fixes `\begin{array}` column count mismatches
  - `_fix_unbalanced_braces()`: depth-walk algorithm to balance `{`/`}`
  - `_fix_unbalanced_delimiters()`: balances `\left`/`\right` pairs
  - `_is_latex_safe()`: final gate — rejects still-broken OCR
- **Table cell OCR** (`_ocr_cell()`): improved preprocessing
  - 300 DPI rendering, 2pt border inset, 20% white padding
  - Uses `find_tables()` for bbox access to OCR empty cells
- **Pipeline reorder**: Canon moved from "Stage 2.5" to Stage 3
  - Old: Extract → Parse → Normalize → Canon → Render → Compile
  - New: Extract(1) → Parse(2) → Canon(3) → Normalize(4) → Render(5) → Compile(6)
- Updated `requirements.txt`: added `nougat-ocr` as optional dependency

### 2026-03-21 — pix2tex equation OCR, math cleanup, conclusions fix
- `extractor/pdf_extractor.py`: Added pix2tex (LatexOCR) as PRIMARY equation OCR
  - pix2tex outputs LaTeX directly (e.g. `\frac{df}{dx}`) — much better than Tesseract for math
  - Tesseract is automatic fallback if pix2tex not installed
  - Lazy-loads pix2tex model (heavy init — only loaded once per run)
  - PyMuPDF path: OCR's image blocks (type=1) inline, injecting LaTeX into text flow
  - pdfplumber path: OCR's extracted images, flags equations for parser injection
  - `_ocr_equation()` → (latex, source) with pix2tex-first fallback chain
  - `_is_valid_equation_ocr()` validates output (pix2tex trusted more, Tesseract stricter)
  - `_wrap_latex()` wraps pix2tex output in $...$ for inline rendering
- `normalizer/cleaner.py`: Added inline math pattern fixes (step 7-8 in pipeline)
  - `_fix_decimal_spaces()`: "3 . 1415" → "3.1415"
  - `_fix_inline_math_patterns()`: handles superscripts, subscripts, log(), O() notation
    - `a2 + b2 = c2` → `$a^{2} + b^{2} = c^{2}$` (equation-context detection)
    - `log(xy)` → `$\log(xy)$`, `O(n2logn)` → `$O(n^{2} \log n)$`
    - `E = mc2` → `$E = mc^{2}$`, `i2 = -1` → `$i^{2} = -1$`
    - Matrix subscripts: `a ij` → `$a_{ij}$` (with English word guard)
  - Table cells now processed through `_clean_with_math()` (were `_clean()` only)
- `parser/heuristic.py`: Fixed "References" at end of paragraph leaking into sections
  - Detects `\bReferences\s*$` at end of body text → splits and sets `in_refs=True`
  - Fixes CONCLUSIONS section body being replaced with reference entries
  - Added ISSN line detection and running header patterns to `_is_metadata_line()`
  - Author extraction: strip affiliation-prefix words ("Technical", "National")
  - Author extraction: break after mid-block Abstract detection (prevents false positives)
  - Author extraction: backfill previous author's affiliation from embedded block text
- `canon/builder.py`: Added `_is_bad_author_name()` validation
  - Rejects names starting with articles ("The"), containing affiliation keywords
  - Rejects very short names and all-caps acronym pairs
  - 2-word titles now accepted in `_is_plausible_title()` (was 3+)

### 2026-03-21 — Equation separation, space recovery, author/ref fixes
- `normalizer/cleaner.py`: Added `_separate_numbered_equations()` as step 6 in `_clean_with_math`
  - Detects collapsed equation runs `(1)...(2)...(3)` in math-heavy paragraphs
  - Splits into separate `\n\n`-separated paragraphs so each equation renders on its own line
  - Safety guards: requires 3+ equation numbers AND `$...$` math present (avoids prose refs)
  - Strips leading `$\cdot$` / `|` column-separator artifacts from split equation paragraphs
- `extractor/pdf_extractor.py`: Added char-based space recovery for space-stripped fonts
  - `_recover_spaces_from_chars(page)`: uses pdfplumber `page.chars` x-position gaps to
    reconstruct word boundaries (gap > 40% avg char width = word boundary)
  - `_fallback_blocks`: auto-detects space-stripped encoding (avg word length > 12) and
    switches to char-based reconstruction
- `parser/heuristic.py` — three fixes:
  - **Title continuation**: when title doesn't end with terminal punctuation, checks if
    next line is a subtitle fragment and appends it (handles 2-line titles)
  - **Author false positives**: `_looks_like_author` now rejects words with hyphens
    ("IEEE-Compliant") and all-caps acronyms ≥ 3 chars ("IEEE", "NLP")
  - **`N.` reference style**: `_extract_refs_from_blocks` now detects `1. Author...`
    in addition to `[1] Author...`; requires ≥ 20 chars after N. to avoid "202. Springer."

### 2026-03-20 — Canonical Structure Builder
- Added `canon/` package: `models.py`, `builder.py`, `features.py`, `classifier.py`
- `CanonicalDocument`: wraps every Document field with confidence score + source tag
- `build_canonical()`: validate → repair (with fallback chains) → score → cross-validate
- `is_renderable()` gate in `pipeline.py`: bad documents never reach Jinja2 renderer
- `repair_log` on every run: visible in `logs/pipeline_latest.log`
- Optional `classifier.py`: sklearn LinearSVC path for future ML-based line typing
- Updated `pipeline.py`: inserted canon stage between Normalize and Render

### 2026-03-19 — Major text-only parser fixes
- Title extraction: scoring heuristic, no longer blindly takes first line
- Author extraction: handles superscript digits, interpunct-separated lists
- Page header filtering: `_is_metadata_line()` catches journal headers, DOIs, copyright
- Reference extraction: author-year citation fallback (91 refs on FedBNR paper)
- Sections reduced 54→20, references 0→91 on test paper

### 2026-03-18 — Initial project + MVP
- Full 5-stage pipeline, 6 templates, end-to-end working
- Font-aware and text-only parsing modes
- Created CLAUDE.md