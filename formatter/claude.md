# CLAUDE.md — ai-paper-formatter Project Memory
> Read this file at the start of every session. Update it when structural changes are made.
> Last updated: 2026-04-07 — Position-aware formula placement, equation numbering, subscript fixes

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
    shared.py                # Shared utilities: MATH_CHARS, thresholds, count_real_words, latex_relpath
    pipeline.py              # Orchestrator — 6 stages + canon gate
    logger.py                # Rotating + latest log
  extractor/
    pdf_extractor.py         # PyMuPDF primary, pdfplumber tables only, OCR orchestration
    pix2tex_worker.py        # Subprocess worker — pix2tex LaTeX OCR (single image)
    pix2tex_batch_worker.py  # NEW: batch worker — load model once, process all images
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

9. **pix2tex confidence filter**: always discard FormulaBlocks with confidence < 0.45.
   Bad OCR output inserted verbatim into LaTeX is worse than no formula at all.
   The `_score_ocr_quality()` heuristic in math_extractor catches prose-as-math garbage.

10. **math_extractor detection must be conservative.** A false positive (cropping body text
    and sending it to pix2tex) wastes ~10s per crop AND produces garbage that breaks pdflatex.
    Keep `MIN_MATH_CHARS >= 5`, `MIN_MATH_RATIO >= 0.08`, `MAX_BLOCK_CHARS <= 300`.

11. **Image-based equations use OCR-or-save pattern.** `_process_equation_image()` always
    saves the equation image to disk. If OCR succeeds → `latex` field set. If OCR fails →
    `image_path` field set. Templates handle both via conditional rendering. Never discard
    an equation image just because OCR failed.

12. **Image classification uses size heuristics.** `_classify_image()` uses height, width,
    area, and aspect ratio. Equation images are typically < 120pt tall. Figures are > 100pt
    tall with area > 15000pt². Very small images (< 15pt) are skipped.

13. **Table text dedup via bbox overlap.** `_get_table_bboxes()` runs pdfplumber before
    fitz extraction. Text blocks with ≥ 50% area overlap with a table are skipped from
    body text to prevent table content appearing twice.

14. **Always use `alpha=False` in pixmap rendering.** `page.get_pixmap(alpha=False)` composites
    onto white background. Without this, SMask images (common in equation PDFs) produce black
    backgrounds that appear as "black spots" in the output PDF.

15. **Batch OCR preferred over per-image OCR.** `pix2tex_batch_worker.py` loads the model once
    and processes all equation images. The single-image worker (`pix2tex_worker.py`) reloads
    the model (~10s) per call. Always use batch OCR via `_batch_ocr_equations()` first.

16. **MATH_SYMBOLS values may contain `$...$`.** When adding new entries to `MATH_SYMBOLS` in
    `normalizer/cleaner.py`, values already wrapped in `$...$` (e.g. `"$\\oplus$"`) must NOT be
    double-wrapped. The normalizer checks for this pattern and skips re-wrapping.

17. **SMask xref must be composited.** `get_images(full=True)` returns a tuple where index 1 is
    the `smask_xref`. When non-zero, call `_composite_with_smask(pdf, xref, smask_xref, raw_bytes)`
    to reconstruct RGBA from fitz.Pixmap and composite onto white via PIL. Without this, transparent
    areas in equation/figure images appear as black rectangles.

18. **Table cell images use `\CELLIMG{}` markers.** `_render_cell_image()` detects page images
    overlapping table cell bboxes and renders them as 200 DPI PNGs. Empty cells get
    `\CELLIMG{path}` text which `_escape_table_cell()` in the renderer converts to
    `\includegraphics[max height=1.5cm]{path}`.

19. **Clean corrupted aux files before pdflatex.** Null bytes (^^@) from previous failed runs
    persist in `.aux`/`.out`/`.toc` files. The compiler scans for `\x00` bytes and removes
    corrupted files before each compilation to prevent "invalid character" errors.

20. **Skip equation images inside tables.** `_extract_all_page_images()` and `_detect_formula_regions()`
    both accept `table_bboxes` and skip images/blocks that overlap table regions. Table equations
    are already handled by `_render_cell_image()` → `\CELLIMG{}` markers. Without this, every table
    equation would appear TWICE: once in the table and once in "Key Equations".

21. **OCR confidence threshold is 0.60.** Raised from 0.45. Below 0.60, the image fallback is used
    instead of inserting garbage LaTeX. The scorer penalizes: `\mathrm{}` abuse, `\to`/`\rightarrow`
    artifacts, `\mathcal` spam, rare Greek (`\Xi`), plain letter runs, and garbage words
    ("pout", "REFERENCES", etc.). Rewards: `\frac`, `\int`, `\sum`, `^{}`, `_{}`, `=` sign.

22. **Equation numbers matched by y-proximity.** `_collect_equation_numbers()` finds standalone
    `(N)` text blocks, `_match_equation_numbers()` pairs them to formulas within 30pt y-distance.
    Each number used at most once. Stored in `FormulaBlock.equation_number`, rendered as `\tag{N}`.

23. **Formula placement is sequential, not interleaved.** `_build_content_blocks()` places
    formula blocks after the section's text paragraphs in position order. Do NOT re-introduce
    "even interval" or other artificial distribution — it scatters formulas next to unrelated text.

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

## Known Issues (as of 2026-03-26)

1. **Overfull hbox** — wide tables overflow column width despite `\resizebox` for >5 columns
2. **ACM .cls file** — needs verification that acmart-tagged.cls is correct version
3. **Multi-column extraction** — hyphenation artifacts from column layout in double-column PDFs
4. **Reference cleaning** — incomplete; some entries still contain in-text citation noise
5. **Table cell images (needs fitz testing)** — `_render_cell_image()` implemented but untested with PyMuPDF; overlap detection may need tuning for fitz's image coordinate system
6. **Acta Avionica logo** — misclassified as equation (minor)
7. **pix2tex borderline garbage** — some structurally valid but semantically wrong LaTeX (e.g. `l^{*} p \eta \frac{...}{}`) can still pass the 0.60 threshold if it has `\frac`, `^{}` etc. Table overlap check prevents most of these from appearing in output.

---

## Changelog

### 2026-04-07 — Position-aware formula placement, subscript fixes
- **Problem (formulas dumped at end)**: Previous fix placed all formulas after section text. Reverted
  to position-aware interleaving: each formula's `(page, bbox_y)` computes its fractional position
  within the section, then it's placed after the corresponding paragraph.
- **Problem (eigenvalue λ(A) broken)**: `_GREEK_SUBSCRIPT_RE` matched the `e` from "eigenvalue" as a
  base variable, producing `eigenvalu$e_{\lambda(A)}$`. Fixed with `(?<![a-zA-Z])` lookbehind and
  updated quick-bail check to verify base letter is standalone.
- **Problem ($\lambda$(A) split)**: After Greek→LaTeX conversion, `λ(A)` became `$\lambda$(A)` — the
  argument was left outside math mode. Added Step 3 in `_merge_adjacent_math()` that absorbs trailing
  `(args)` into the math span: `$\cmd$(args)` → `$\cmd(args)$`.

### 2026-04-06 — Formula numbering preservation + placement fix
- **Problem (equation numbers lost)**: OCR-extracted formula images (FormulaBlocks) had no equation
  numbers. The original `(7)`, `(8)` etc. from the input document were not carried through to output.
- **Problem (formula placement)**: `_build_content_blocks()` in renderer used "even intervals" formula
  `floor((i+1) * n_para / (n_fb+1))` to scatter formulas among paragraphs — artificial placement
  that put formulas next to unrelated text.
- **FormulaBlock model** (`core/models.py`): Added `equation_number: str = ""` field to store the
  original equation number from the input document (e.g. "7" for equation `(7)`).
- **Equation number extraction** (`extractor/pdf_extractor.py`):
  - Added `_collect_equation_numbers(text_dict)`: scans page text blocks for standalone `(N)` patterns,
    returns list of `(y_center, number_str)` sorted by position.
  - Added `_match_equation_numbers(formulas, eq_nums)`: matches equation numbers to formula blocks
    by y-position proximity (max 30pt distance). Each number used at most once (closest wins).
  - Wired into `_detect_formula_regions()` and `_extract_all_page_images()`.
  - `_extract_all_page_images()` now accepts optional `text_dict` parameter for equation number matching.
  - `_detect_formula_regions()` now includes `bbox_y` in returned formula dicts (was missing).
- **Renderer** (`renderer/jinja_renderer.py`):
  - `_fb_to_dict()` now passes `equation_number` through to template context.
  - `_build_content_blocks()` rewritten: formulas placed sequentially after section text in position
    order, instead of scattered at artificial even intervals. Simple, predictable, correct.
- **All 6 templates** (ieee, acm, springer, elsevier, apa, arxiv):
  - Equation blocks now render OCR latex when available (previously only images were rendered).
  - When `equation_number` is present, equations wrapped in `\begin{equation*}\tag{N}...\end{equation*}`
    to display the original `(N)` numbering.
  - Without equation number: images render in `\begin{center}` (unchanged), latex in `equation*`.
- **Text-based equations** (normalizer): Already had `\tag{N}` from `_convert_numbered_equations()` — no change needed.

### 2026-04-06 — Code review fixes: shared utilities, bug fixes, deduplication
- **Created `core/shared.py`**: Single source of truth for cross-module constants and utilities.
  Contains: `MATH_CHARS` (487 chars), `OCR_CONFIDENCE_THRESHOLD`, `TABLE_CELL_OCR_THRESHOLD`,
  `OCR_RENDERER_THRESHOLD`, `MATH_STOP_WORDS`, `count_real_words()`, `latex_relpath()`.
- **Operator precedence bug** (cleaner.py:341): `or` vs `and` without parens — fixed.
- **Caption matching bug** (pdf_extractor.py): `dist = abs(cy - 0)` hardcoded → uses actual `fig_y`.
- **Figure label collision** (pdf_extractor.py): `len(figures)` after append → pre-computed `fig_idx`.
- **Display math escaping** (jinja_renderer.py): `$$` treated as two `$` toggles → proper detection.
- **Compiler log overwrite**: pass 2 overwrote pass 1 → append mode for pass 2.
- **OCR budget state leak**: mutable global dict → `_OcrBudget` class with `reset()`.
- **MATH_CHARS divergence**: 3 definitions → consolidated in `shared.py`.
- **count_real_words duplication**: 5+ copies → single function in `shared.py`.
- **PIL imports**: 7+ local imports → module-level guarded import.
- **Path relativization**: 4 instances → `latex_relpath()` in `shared.py`.
- **FormulaBlock dict/dataclass mix**: isinstance checks → upfront conversion in pipeline.

### 2026-03-26 — Table rendering, header formulas, author extraction, Key Equations dedup
- **Problem (table header formulas)**: First table row (e.g. `a2 + b2 = c2`) treated as header
  and cleaned with `_clean()` instead of `_clean_table_cell()` — formula patterns never applied.
- **Problem (row merging)**: Only header row had `\hline` — data rows used `\\` without separator,
  causing rows with tall equation images to appear merged.
- **Problem (table cell borders)**: `_render_cell_image()` cropped at exact cell bbox, capturing
  table grid lines as visible borders around equation images in cells.
- **Problem (missing authors)**: "SZABÓ*\nPeter" format (SURNAME on one line, given name on next)
  wasn't detected — `_is_plausible_author()` requires 2+ words per line.
- **Problem (DOI as title)**: "Volume XXIV... DOI:" line wasn't detected as metadata, so it
  became the title instead of "THE FORMULAS".
- **Table cell crop inset** (`pdf_extractor.py`): Changed `pad=1` to `inset=2` — crop now
  excludes 2pt border on all sides, removing table grid lines from cell equation images.
- **Equation dedup for unknown-bbox images** (`pdf_extractor.py`): When equation images lack
  a rendered bbox (embedded as XObjects), skip them if the page has tables — they're handled
  by CELLIMG already.
- **Author SURNAME/GivenName merging** (`parser/heuristic.py`): Pre-pass on block lines
  combines adjacent single-word UPPERCASE + single-word Titlecase into "GivenName SURNAME".
- **Metadata detection** (`parser/heuristic.py`): Added `Volume XXIV...` and `DOI:...anywhere`
  patterns to METADATA_PATTERNS. DOI lines now correctly filtered before title extraction.
- **Author zone triggers** (`parser/heuristic.py`): Lowered font threshold from 1.2→1.15x body
  size. Added Trigger 3: block matching detected title text enters author zone.
- **Table header formula fix** (`normalizer/cleaner.py`): Changed `table.headers` cleanup from
  `_clean()` to `_clean_table_cell()` — header cells now get formula pattern matching.
- **Table row separators** (`renderer/jinja_renderer.py`): Added `\hline` after every data row
  (was header-only). Prevents visual row merging when cells contain tall equation images.

### 2026-03-26 — Table equation dedup, OCR quality overhaul
- **Problem (duplicate equations)**: Equation images inside the table were extracted BOTH as
  `\CELLIMG{}` table cell images AND as standalone `FormulaBlock`s in "Key Equations" — double rendering.
- **Problem (garbage OCR)**: pix2tex produced garbage LaTeX (arrows, wrong symbols, prose) that
  passed the 0.45 confidence filter and rendered as nonsensical equations.
- **Table equation dedup** (`pdf_extractor.py`):
  - `_extract_all_page_images()` now accepts `table_bboxes` parameter
  - Equation images overlapping table regions are skipped (already handled by CELLIMG)
  - `_detect_formula_regions()` also skips text blocks inside table regions
- **OCR quality scorer overhaul** (`_score_ocr_quality()`):
  - New penalties: `\to`/`\rightarrow` artifacts, `\mathcal` spam (≥2-3 uses),
    rare Greek (`\Xi`, `\Upsilon`), plain letter runs (>25-40% of content),
    garbage words ("pout", "REFERENCES", "equation"), isolated single-letters without `=`
  - New rewards: `\sqrt`, `\infty`, `\pi`, `\sigma`, balanced equations
  - Threshold raised from 0.45 → 0.60 across all OCR paths (batch + single-image + text-region)
- **Pipeline filter** (`pipeline.py`):
  - Formula blocks now accepted if: OCR confidence ≥ 0.60, OR image-only fallback (no latex)
  - Image-only formulas rendered as `\includegraphics` (better than garbage LaTeX)

### 2026-03-26 — SMask xref compositing, table cell images, corrupted aux cleanup
- **Problem (black spots)**: `fitz.extract_image(xref)` returns raw RGB; alpha mask is in a SEPARATE
  `smask_xref` (index 1 of `get_images(full=True)` tuple). Without reconstructing RGBA from both
  xrefs and compositing onto white, transparent areas render as black rectangles.
- **Problem (empty table cells)**: Table cells containing equation images (derivative, gravity, etc.)
  rendered as empty — no image detection for table cell regions.
- **Problem (corrupted aux)**: Null bytes from failed pdflatex runs persisted in `.aux` files,
  causing "invalid character" errors on subsequent runs.
- **SMask xref compositing** (`pdf_extractor.py`):
  - Added `_composite_with_smask(pdf, xref, smask_xref, raw_bytes)` — reconstructs RGBA from
    `fitz.Pixmap(pdf, xref)` + `fitz.Pixmap(pdf, smask_xref)`, composites via PIL onto white
  - Image extraction loop now extracts `smask_xref = img_info[1]` and calls compositing early
  - `_composite_on_white()` — PIL fallback for RGBA images without separate smask
  - All `page.get_pixmap()` calls now use `alpha=False` (composites on white by default)
- **Batch pix2tex worker** (`pix2tex_batch_worker.py`) — NEW FILE:
  - Loads model ONCE, processes all image paths from CLI args
  - Outputs JSON array: `[{"path": "...", "latex": "..."}, ...]`
  - ~10x faster than per-equation subprocess spawning
- **Batch OCR integration** (`pdf_extractor.py`):
  - `_batch_ocr_equations()`: collects all equation images, runs batch worker
  - `_run_batch_ocr_worker()`: subprocess runner with scaled timeout (60s + 5s/img)
  - `_process_equation_image()`: defers OCR to batch phase (saves image only)
  - Falls back to single-image workers if batch unavailable
- **pdfplumber image extraction** (`pdf_extractor.py`):
  - `_extract_with_pdfplumber()` now extracts images via `page.crop().to_image(resolution=200)`
  - Classifies images as equation/figure/skip using `_classify_image()`
  - Also calls `_batch_ocr_equations()` for OCR
  - Previously returned empty `figures: [], formula_blocks: []`
- **Unicode math fixes** (`normalizer/cleaner.py`):
  - Added 15+ missing symbols to `MATH_SYMBOLS`: − (U+2212), ⊕, ⊗, ⊖, ∘, ⟨, ⟩, ′, ″, ∝, ≡, ≅, ≪, ≫
  - Fixed double-wrapping bug: symbols already containing `$...$` were wrapped again
- **Table cell equation images** (`pdf_extractor.py`, `jinja_renderer.py`):
  - Rewrote `_extract_tables_pdfplumber()` to use `find_tables()` for cell-level bboxes
  - Added `_build_cell_bbox_grid()`: maps (row, col) → cell bbox from pdfplumber's flat cell list
  - Added `_render_cell_image()`: checks page image/cell bbox overlap, renders as 200 DPI PNG
  - Empty cells with overlapping images get `\CELLIMG{path}` marker text
  - Renderer `_escape_table_cell()` detects `\CELLIMG{path}` → `\includegraphics[max height=1.5cm]`
- **Corrupted aux cleanup** (`latex_compiler.py`):
  - Before compilation, scans `.aux`, `.out`, `.toc`, `.lof`, `.lot`, `.log` for null bytes
  - Removes files containing `\x00` to prevent "invalid character" errors from previous runs

### 2026-03-25 — Image extraction overhaul, OCR-or-save fallback, table dedup
- **Problem**: Formulas.pdf has equations as embedded images, not Unicode text.
  `_detect_formula_regions()` only scanned text blocks → 0 formulas found.
  Images were also missed (0 figures). Table content was duplicated in body text.
- **Image extraction rewrite** (`pdf_extractor.py`):
  - Replaced block-level image iteration with `page.get_images(full=True)` as primary
  - Added `_classify_image()`: size/aspect ratio heuristic → equation vs figure vs skip
  - Added `_process_equation_image()`: OCR-or-save pattern (pix2tex → nougat → save image)
  - Added `_extract_all_page_images()`: processes all images per page
  - Added `_find_image_y_position()`: looks up image bbox for ordering
- **Table dedup** (`pdf_extractor.py`):
  - `_get_table_bboxes()`: pre-extracts table bounding boxes from pdfplumber
  - `_bbox_overlaps_any()`: skips text blocks with ≥50% overlap with table regions
- **FormulaBlock.image_path** (`core/models.py`):
  - Added `image_path: str` and `bbox_y: float` fields
  - When OCR fails, equation image is saved and path is set (confidence=0.5)
- **Template update** (all 6):
  - Added `\usepackage[export]{adjustbox}` for max width/height
  - Formula blocks render as `\begin{equation}` (latex) or `\includegraphics` (image_path)
  - Section renamed from "Equations" to "Key Equations"
- **Renderer** (`jinja_renderer.py`):
  - `_fb_to_dict()` now converts image_path to relative path
  - `_figure_to_dict()` normalizes paths for cross-platform LaTeX
- **Normalizer** (`normalizer/cleaner.py`):
  - Table cells now use `_clean_table_cell()` with formula pattern recognition
  - Added `_TABLE_FORMULA_PATTERNS`: regex for a²+b²=c², E=mc², i²=-1, log formulas
  - Table cells get full math cleanup (was `_clean()` only, now `_clean_with_math()` + patterns)

### 2026-03-22 — math_extractor v2: stricter detection + OCR quality scoring
- **Root cause**: math_extractor was flagging body text paragraphs as formula regions
  - `MIN_MATH_CHARS=2` meant any paragraph with α and β got cropped & OCR'd
  - Font hints too broad ("math", "symbol", "stix") — STIX is used for regular glyphs
  - No block-length filter → entire paragraphs sent to pix2tex → garbage output
  - `confidence` hardcoded to `1.0` → 0.45 threshold filter was useless
  - No quality check on OCR output → `\mathrm{Keywords~federning}` passed through
  - 19+ subprocess calls at ~10s each = 3+ min of wasted processing
- **Detection fixes** (`math_extractor.py`):
  - `MIN_MATH_CHARS` raised from 2 → 5
  - Added `MIN_MATH_RATIO = 0.08` (math chars must be ≥8% of block chars)
  - Added `MAX_BLOCK_CHARS = 300` (skip long body paragraphs)
  - Narrowed font hints to TeX-only: `cmex`, `cmsy`, `cmmi`, `euler`
  - Font-only detection now also requires ≥2 math chars
  - Added `MAX_PER_PAGE = 8` and `MAX_TOTAL = 40` caps
  - Added minimum crop size filter (`MIN_CROP_W=60`, `MIN_CROP_H=20`)
- **Quality scoring** (`_score_ocr_quality()`):
  - Replaces hardcoded `confidence=1.0` with heuristic 0.0–1.0 score
  - Penalizes high `\mathrm{...}` ratio (prose wrapped as fake math)
  - Penalizes tilde-separated words inside `\mathrm{}` (pix2tex space encoding)
  - Penalizes `\scriptstyle` wrapping large blocks
  - Bonuses for real math structures: `\frac`, `\int`, `\sum`, `^`, `_{}`
  - Threshold: confidence < 0.45 → rejected (logged as REJECTED in debug log)
- **Pipeline filter** (`pipeline.py`):
  - Added `confidence >= 0.45` filter on formula_blocks before attaching to Document
  - Logs count of discarded low-confidence blocks

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

don't write repititve code build tools that will do it , for actions that require same thing over and over again and dont write garbage code