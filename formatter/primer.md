# primer.md — Quick Reference
> Last updated: 2026-03-26 — SMask xref compositing, table cell images, corrupted aux cleanup

## Current Status
- **Project rebuilt from scratch** — clean modular architecture
- All 6 templates working: ieee, acm, springer, elsevier, apa, arxiv
- PDF + DOCX inputs supported
- **Equation image extraction**: get_images() primary, OCR-or-save fallback
- **Black spot fix**: SMask xref compositing via `_composite_with_smask()` + `alpha=False`
- **Table cell images**: `_render_cell_image()` detects equation images in table cells → `\CELLIMG{}` markers
- **Batch pix2tex**: Loads model once for all equations (was per-equation subprocess)
- **pdfplumber image extraction**: Full fallback when PyMuPDF unavailable
- **Corrupted aux cleanup**: Null-byte detection in .aux files before pdflatex
- Canon validation gate prevents broken docs from reaching LaTeX

## Recent Changes (2026-03-26)

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
  2. Section.formula_blocks rendered inline (after figures, before next section)
  3. Any unplaced formulas → "Key Equations" section at end of document

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

## Tasks Pending
- [ ] Test batch pix2tex on Windows with full dependencies
- [ ] Acta Avionica logo misclassified as equation (minor)
- [ ] Multi-column layout: improve hyphenation artifact handling
- [ ] Reference cleaning: reduce in-text citation noise
