# primer.md — Quick Reference
> Last updated: 2026-04-16 — IEEE author side-by-side layout fix

## Current Task (2026-04-16 v2) — LaTeX auto-numbering + paragraph-break preservation
**Problem A (duplicate numbers)**: Two formulas both labeled `(2)(2)`. The
normalizer's `_convert_numbered_equations` produced `\begin{equation*}\tag{N}`
using the original printed number, while the pipeline independently assigned
sequential numbers 1..N to image-based FormulaBlocks via `\tag{N}` in the
template. Two parallel numbering pools → collisions.

**Problem B (stacked formulas, missing paragraph breaks)**: In the Goldbach
section, formulas (5) and (6) rendered back-to-back at the section's end and
three separate paragraphs merged into one. Root cause was in
`_merge_orphan_symbol_lines` (normalizer): standalone `(7)` / `(8)` equation
number lines were classified as orphan math symbols and merged into the
previous line, but when the previous line was blank (the `\n\n` paragraph
break) the merge collapsed it — destroying the paragraph boundary. The
renderer then saw a single paragraph and placed both formulas after it.

**Fix**:
1. `normalizer/cleaner.py::_merge_orphan_symbol_lines`:
   - Standalone `(N)` equation-number lines are now dropped (not merged).
   - Any orphan whose preceding accumulated line is blank is also dropped
     instead of being merged upward, preserving `\n\n` paragraph breaks.
2. `normalizer/cleaner.py::_convert_numbered_equations`: switched output from
   `\begin{equation*}\tag{N}` to `\begin{equation}` (auto-numbered).
3. `renderer/jinja_renderer.py::_split_para_blocks`: now accepts both
   `equation` and `equation*` environments as raw LaTeX pass-through.
4. `template/ieee/template.tex.j2`: formula-block rendering switched from
   `equation*`+`\tag{N}` / `\hfill(N)` to `\begin{equation}...\end{equation}`
   for LaTeX text equations and image formulas (image wrapped in
   `\vcenter{\hbox{\includegraphics{...}}}` to sit inside math mode).
5. `core/pipeline.py`: removed sequential `fb.equation_number` assignment —
   LaTeX's equation counter now owns numbering, guaranteeing sequential,
   collision-free numbers across both normalizer-produced equations and
   image-based formula blocks.

Result on Formulas.pdf: equations now render (1)…(6) in document order with
no duplicates, and formulas interleave correctly with their surrounding
paragraphs in the Goldbach section.

## Previous task (2026-04-16) — IEEE authors always side-by-side
**Problem**: `\IEEEauthorblockN` + `\and` in the IEEE template auto-wrapped
authors to stacked rows whenever their combined affiliation width exceeded
`\textwidth`. Formulas.pdf (Košice affiliations) triggered this — two authors
rendered one-above-the-other instead of in two columns like the source PDF.

**Fix** (`template/ieee/template.tex.j2`):
- Replaced `\IEEEauthorblockN`/`\IEEEauthorblockA` + `\and` with a row of
  `\begin{minipage}[t]{col_width\textwidth}…\end{minipage}` blocks separated
  by `\hfill`.
- `col_width = 0.96 / (authors|length)` (Jinja `set` with round filter), so
  the layout scales for 1–N authors.
- Each minipage holds `\textbf{name}` + footnotesize affiliation lines with
  `\\` line breaks. `\\` is safe inside a minipage (unlike a tabular where
  it ends the row).
- Result: authors always sit side-by-side in equal columns regardless of
  affiliation length.

## Previous task (2026-04-14 v5) — Logo on page 0 + block-split fallback
**Problem carry-over**: Acta Avionica masthead was STILL classified as an
equation (stealing number (1)). It only appears on page 0, so the recurring-
xref skip didn't catch it. Goldbach section was still one merged paragraph
because equation bbox_y was zero for embedded XObject images, so the per-
formula y-span splitter couldn't fire.

**Fix**:
1. `_classify_image` now takes `bbox_y`. Any image on page 0 with
   `bbox_y < 120` and area < 20000 is skipped as a masthead. All three call
   sites (rendered-xref branch, inline-image branch, pdfplumber branch) pass
   the image's y-top through.
2. `_split_block_by_formulas` adds a **line-gap fallback**: computes median
   line height for the block and splits whenever the gap between two
   consecutive lines exceeds 1.6× median (or 14pt floor). This catches image-
   induced paragraph breaks even when the image's bbox_y is missing from
   upstream data. Formula-span crossing still triggers splits too.
3. Formula overlap with a line gap is also a split signal (more permissive
   than the strict "completely between centers" rule).

## Previous task (2026-04-14 v4) — Image misclassification fix
**Problem**: Acta Avionica journal logo (header) and the satellite-orbit Figure 1
were being classified as equations and assigned equation numbers (1), (3).
Real equations then started at (7) instead of (1).

**Fix** in `pdf_extractor.py`:
1. **Recurring-xref logo skip**: pre-pass counts each image xref across all
   pages. Any xref appearing on 2+ pages is a journal logo / running header →
   `skip_xrefs` set passed into `_extract_all_page_images()`, which drops them
   before classification.
2. **Caption-based figure override**: new `_find_figure_caption_near(text_dict,
   bbox)` scans text blocks for "Figure N" / "Fig. N" within 80pt of the image
   bbox. When a match is found, classification flips from 'equation' to 'figure'.
3. `_classify_image` signature unchanged — the override wraps it in
   `_extract_all_page_images` so the core size heuristic stays pure.

## Previous task (2026-04-14 v3) — Paragraph-formula interleaving fix
**Problem**: Goldbach section in output showed three paragraphs merged into one,
formulas (7) and (8) dumped at the end of the section instead of between their
introducing and concluding sentences. Root cause: PyMuPDF groups lines above and
below an inline equation image into a single text block. Parser then sees one
paragraph → renderer has nowhere to place the formula except at section end.

**Fix**: In `pdf_extractor._extract_with_fitz()`:
- Reordered per-page loop: `_extract_all_page_images` runs BEFORE text-block
  loop so formula y-positions are known first.
- New `_split_block_by_formulas(block, eq_y_spans)`: for each fitz text block,
  splits its `lines` into runs separated by any equation y-span that lies
  between two consecutive line centers. Each run becomes its own sub-block
  with a bbox computed from the run's line bboxes.
- New `_run_to_subblock(run_lines, fallback_bbox)`: helper that converts a
  run of fitz line dicts back into the `{text, bbox, font, size}` shape the
  rest of the pipeline expects.

Result: paragraphs above and below an inline formula are now separated by
`\n\n` in `section.body`, with distinct `body_positions` entries. The
renderer's `_match_formulas_to_paragraphs` then places each formula after
the correct paragraph.

## Previous task (2026-04-14 v2)
User complaint: first formula rendered as (8), formulas duplicated in Key Equations,
author blocks wrapping to stacked rows.

**Fix:**
1. **Printed-number matching disabled for this doc**: extractor clears any matched
   equation_number (was assigning wrong numbers to logos/figures misclassified as
   equations). `_match_equation_numbers` max_y_dist reverted to 50pt.
2. **Sequential numbering in pipeline**: `pipeline.py` assigns 1..N in (page, y)
   reading order AFTER distribution to sections. First formula = (1), always.
3. **Key Equations duplicate dropped**: `doc.formula_blocks = []` — templates see
   no global list so the summary table is skipped. Inline placement only.
4. **IEEE author template tightened**: removed stray trailing `\\` and `\textit{}`
   that made blocks taller than column width → they wrap to rows. Now more compact.

## Previous task (2026-04-14 v1, superseded above)
Fix formulas showing as images instead of numbered LaTeX + make pix2tex and nougat
actually run together. Three bugs:

1. **Nougat never invoked** — availability probe had 10s timeout; nougat import
   is heavier than that. Fix: `pdf_extractor._check_ocr_available()` now uses 60s
   for nougat, and on timeout assumes-available so the batch worker itself decides.
2. **Every OCR result dropped at render time** — `OCR_RENDERER_THRESHOLD=0.80` in
   `core/shared.py` was higher than the scorer tops out at (~0.75). Lowered to 0.60.
   `_is_simple_correct_latex()` still gates structural quality.
3. **Equation numbers missing** — `_match_equation_numbers()` y-distance widened
   50→80pt. New `_auto_number_formulas()` pass assigns sequential numbers to any
   formula whose printed "(N)" couldn't be matched, so Key Equations never shows `---`.

Dual-engine winner is now logged at INFO (was DEBUG) — `grep "Dual OCR" logs/pipeline_latest.log`
to verify both engines ran.



## Current Status
- **Project rebuilt from scratch** — clean modular architecture
- All 6 templates working: ieee, acm, springer, elsevier, apa, arxiv
- PDF + DOCX inputs supported
- **Position-aware formula placement**: Paragraphs carry `(page, y)` positions from parser → renderer places formulas after the correct paragraph
- **Equation image extraction**: get_images() primary, OCR-or-save fallback
- **Black spot fix**: SMask xref compositing via `_composite_with_smask()` + `alpha=False`
- **Table cell images**: `_render_cell_image()` detects equation images in table cells → `\CELLIMG{}` markers
- **Batch pix2tex**: Loads model once for all equations (was per-equation subprocess)
- **pdfplumber image extraction**: Full fallback when PyMuPDF unavailable
- **Corrupted aux cleanup**: Null-byte detection in .aux files before pdflatex
- Canon validation gate prevents broken docs from reaching LaTeX

## Recent Changes (2026-04-07) — Position-aware formula placement

### Root cause
Formulas were placed at wrong positions because paragraph position data was lost.
The parser concatenated blocks into `section.body` as plain text — the `(page, y)`
of each paragraph was discarded. The renderer then guessed placement using a hacky
fractional formula with a **hardcoded 800pt page height** assumption, producing
wrong results for any non-standard page size or multi-page section.

### Fix: `body_positions` — paragraph position tracking
- **`core/models.py`**: Added `body_positions: list` field to `Section` —
  list of `(page, y)` tuples, one per paragraph in `body` (split on `\n\n`).
- **`parser/heuristic.py`**: `_extract_sections_from_blocks()` now records
  `(page, y_top)` from each block's bbox when appending to `current_body`.
  Uses a helper `_save_current_section()` to avoid repetition across 4 save points.
- **`renderer/jinja_renderer.py`**: `_build_content_blocks()` rewritten:
  - When `body_positions` available and count matches paragraphs: uses
    `_match_formulas_to_paragraphs()` — direct `(page, y)` comparison to find
    the last paragraph before each formula.
  - When positions unavailable or count mismatch: falls back to appending all
    formulas after the last paragraph (safe default, no wrong interleaving).
- **`core/pipeline.py`**: Added `_resync_body_positions()` after Stage 4 (Normalize).
  The normalizer can remove paragraphs (garbage cleanup); this trims/clears
  `body_positions` to stay in sync.

### How it works
```
Parser:  block(page=2, y=100) → body_positions[(2,100)]  → "Para A"
         block(page=2, y=400) → body_positions[(2,400)]  → "Para B"

Formula: FormulaBlock(page=2, bbox_y=250)

Renderer: 250 > 100 (para A) but 250 < 400 (para B)
          → place formula AFTER para A
```

## Previous Changes (2026-03-26)

### SMask XRef Compositing — Deep Black Spot Fix (`extractor/pdf_extractor.py`)
- **Root cause**: `fitz.extract_image(xref)` returns raw RGB; the alpha mask is in a SEPARATE xref (`smask_xref` at index 1 of `get_images(full=True)` tuple). Without compositing, transparent areas render as black.
- **Fix 1 (primary)**: `_composite_with_smask(pdf, xref, smask_xref, raw_bytes)` — uses `fitz.Pixmap` to reconstruct RGBA from main + mask xrefs, then PIL composites onto white
- **Fix 2**: `_composite_on_white()` — PIL composites RGBA images onto white background (fallback)
- **Fix 3**: `page.get_pixmap(alpha=False)` — pixmap rendering composites on white by default
- **Fix 4**: All image extraction paths now extract `smask_xref = img_info[1]` and call `_composite_with_smask` early
- **Fix 5**: Fallback path always runs `_composite_on_white()` on raw bytes

### Table Cell Equation Images (`extractor/pdf_extractor.py`, `renderer/jinja_renderer.py`)
- **Problem**: Table cells with equation images (e.g. derivative, gravity formulas) rendered as empty cells
- **`_extract_tables_pdfplumber()`**: Rewritten to use `find_tables()` for cell-level bboxes
- **`_build_cell_bbox_grid(cells, num_rows, num_cols)`**: Maps (row, col) → cell bbox from pdfplumber's flat cell list
- **`_render_cell_image(page, cell_bbox, page_images, ...)`**: Checks image/cell overlap, renders cell region as 200 DPI PNG
- **`\CELLIMG{path}` marker**: Empty cells with overlapping images get marker text
- **Renderer**: `_escape_table_cell()` detects `\CELLIMG{path}` → converts to `\includegraphics[max height=1.5cm]{path}`

### Corrupted Aux File Cleanup (`compiler/latex_compiler.py`)
- **Problem**: Null bytes (^^@) from previous corrupted pdflatex runs persist in `.aux` files, causing "Text line contains an invalid character" errors
- **Fix**: Before each compilation, scans `.aux`, `.out`, `.toc`, `.lof`, `.lot`, `.log` for null bytes and removes corrupted files

### Batch pix2tex Worker (`extractor/pix2tex_batch_worker.py`) — NEW FILE
- Loads model ONCE, processes all image paths from CLI args
- Outputs JSON: `[{"path": "...", "latex": "..."}, ...]`
- ~10x faster than spawning one subprocess per equation
- Timeout scales: 60s + 5s per image

### Batch OCR Integration (`extractor/pdf_extractor.py`)
- **`_batch_ocr_equations()`**: Collects all equation images, runs batch worker
- **`_run_batch_ocr_worker()`**: Subprocess runner with scaled timeout
- **`_process_equation_image()`**: Now defers OCR to batch phase (save image only)
- Falls back to single-image workers if batch unavailable
- Called at end of both fitz and pdfplumber extraction paths

### pdfplumber Image Extraction (`extractor/pdf_extractor.py`)
- `_extract_with_pdfplumber()` now extracts images via `page.crop().to_image(resolution=200)`
- Uses `_classify_image()` to sort into equation/figure/skip
- Calls `_batch_ocr_equations()` for OCR
- Previously returned empty `figures: []` and `formula_blocks: []`

### Unicode Math Symbol Fixes (`normalizer/cleaner.py`)
- Added 15+ missing math operators: − (U+2212), ⊕, ⊗, ⊖, ∘, ⟨, ⟩, ′, ″, ∝, ≡, ≅, ≪, ≫
- Fixed double-wrapping bug: symbols already containing `$...$` were wrapped again as `$$..$$`

## Previous Changes (2026-03-25)

### Formula Block Placement (`pipeline.py`, `parser/heuristic.py`, all 6 templates)
- **Section.start_page**: New field tracks which page each section heading appears on
- **Section.formula_blocks**: New field — formulas embedded within their source section
- **`_attach_formulas_to_sections()`**: Distributes FormulaBlocks by page-range proximity
- **Template update**: All 6 templates render section-level formula_blocks inline (after figures)

### Image & Formula Extraction Overhaul
- **`get_images()` as primary**: Replaced block-level image iteration with `page.get_images(full=True)`
- **Image classification**: `_classify_image()` uses size/aspect ratio to sort into equation vs figure vs skip
- **OCR-or-save fallback**: `_process_equation_image()` tries pix2tex → nougat → saves raw image
- **FormulaBlock.image_path**: Templates render equation images via `\includegraphics`

### Table Deduplication
- **Table bbox filtering**: pdfplumber table bboxes extracted first, text blocks overlapping ≥50% skipped

## Pipeline (6 stages)
```
Extract → Parse → Canon → Normalize → Render → Compile
  PDF/DOCX   sections    validate    unicode     Jinja2    pdflatex
  → text     → Document  + repair    + math      → .tex    → .pdf
```

## Image Classification Logic
```
_classify_image(w, h):
  if w < 15 or h < 15 or area < 500  → skip (icon/bullet)
  if h >= 100 and w >= 150 and area >= 15000 and w/h < 3.0  → figure
  if h < 120  → equation
  if w < 300  → equation (tall matrix)
  else → figure
```

## Formula Block Rendering
```
FormulaBlock:
  latex: str        # OCR'd LaTeX (if pix2tex/nougat succeeded)
  image_path: str   # saved equation image (fallback)
  confidence: float # 0.0-1.0
  page: int
  label: str
  bbox_y: float     # y-position for ordering

Placement:
  1. FormulaBlocks distributed to nearest Section by page proximity
  2. Renderer interleaves formulas with paragraphs using body_positions
     (each formula placed after the paragraph that precedes it in the source PDF)
  3. Fallback: if body_positions unavailable, formulas go after last paragraph
  4. Any unplaced formulas → forced into last body section

Template renders (per formula_block):
  if fb.latex    → \begin{equation}...\end{equation}
  elif fb.image_path → \includegraphics[max width=0.85\columnwidth]
```

## Run It
```bash
python main.py input/Formulas.pdf --template ieee
python main.py input/paper.pdf --template acm
python main.py input/paper.docx --template springer
```

## Key Rules
1. PyMuPDF is primary extractor; pdfplumber for tables + now also image fallback
2. Canon gate is mandatory before rendering
3. ML OCR must run in subprocesses (segfault protection)
4. OCR output must be sanitized before LaTeX
5. pix2tex confidence < 0.45 → reject OCR, keep image fallback
6. One change at a time
7. Table bboxes from pdfplumber used to filter overlapping fitz text blocks
8. Image classification: equation (< 120pt tall) vs figure (≥ 100pt, area ≥ 15000)
9. All formula images saved to intermediate/figures/ regardless of OCR success
10. **Always use `alpha=False` in pixmap rendering** — prevents SMask black backgrounds
11. **Batch OCR preferred** — load model once, process all images
12. **SMask xref must be composited** — `get_images(full=True)` returns `smask_xref` at index 1; must call `_composite_with_smask()` when non-zero
13. **Table cell images use `\CELLIMG{}` markers** — renderer converts to `\includegraphics`
14. **Clean corrupted aux files before pdflatex** — null bytes from previous runs crash compilation
15. **Skip equation images inside tables** — `_extract_all_page_images()` and `_detect_formula_regions()` check table bboxes; table equations are handled by `\CELLIMG{}` already
16. **OCR confidence threshold is 0.60** — raised from 0.45; borderline OCR falls back to image rendering
17. **Equation images saved with 200 DPI metadata** — prevents LaTeX from rendering at 72 DPI (2-3x too large)
18. **Template uses `max width`/`max height` from adjustbox** — images stay at natural size, only shrink if needed
19. **Arrow commands penalized in OCR scorer** — `\leftrightarrow`, `\longleftrightarrow`, `↔`, `{X}\to` patterns rejected

## Tasks Completed
- [x] Table header formula fix: headers now use `_clean_table_cell` (was `_clean` — skipped formula patterns)
- [x] Table row separators: `\hline` after every row (was header-only — rows looked merged)
- [x] Table cell crop inset: 2pt inset to exclude table border lines from equation images
- [x] Skip equation images on pages with tables when no rendered bbox available
- [x] Author extraction: SURNAME/GivenName pair merging on adjacent lines
- [x] Title fix: DOI/Volume metadata lines detected and filtered before title extraction
- [x] Author zone trigger: title match + lowered font threshold (1.2→1.15)
- [x] Skip equation images inside table regions (prevent double-counting in Key Equations)
- [x] Strengthen OCR quality scorer: `\mathcal` abuse, `\Xi`, plain letter runs, garbage words
- [x] Raise OCR confidence threshold from 0.45 → 0.60 (prefer image fallback over garbage LaTeX)
- [x] SMask xref compositing: `_composite_with_smask()` + `alpha=False`
- [x] Table cell equation images: `_render_cell_image()` + `\CELLIMG{}` markers
- [x] Corrupted .aux file cleanup before pdflatex
- [x] Black spot fix: alpha compositing + pixmap `alpha=False`
- [x] Batch pix2tex worker (model loaded once)
- [x] pdfplumber image extraction fallback
- [x] Unicode math symbols: −, ⊕, ⊗ and 12 more added to normalizer
- [x] Double-wrap fix in MATH_SYMBOLS (symbols already in $...$)
- [x] Author extraction: structured "Authors" heading + multi-line block splitting
- [x] Table-in-text deduplication via bbox overlap detection
- [x] Image extraction via get_images() (replaces block-level iteration)
- [x] OCR-or-save fallback for equation images
- [x] FormulaBlock.image_path field + template rendering
- [x] Embed formula blocks at source location (by page proximity to sections)
- [x] Figure caption detection from nearby text blocks

- [x] Position-aware formula placement: body_positions tracking through parser → renderer

## Tasks Pending
- [ ] Test batch pix2tex on Windows with full dependencies
- [ ] Acta Avionica logo misclassified as equation (minor)
- [ ] Multi-column layout: improve hyphenation artifact handling
- [ ] Reference cleaning: reduce in-text citation noise
