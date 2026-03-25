# primer.md — Quick Reference
> Last updated: 2026-03-25 — Formula placement + figure caption detection

## Current Status
- **Project rebuilt from scratch** — clean modular architecture
- All 6 templates working: ieee, acm, springer, elsevier, apa, arxiv
- PDF + DOCX inputs supported
- **Equation image extraction**: get_images() primary, OCR-or-save fallback
- **pix2tex integration**: subprocess isolation, image fallback when unavailable
- Canon validation gate prevents broken docs from reaching LaTeX

## Recent Changes (2026-03-25 — session 3)

### Formula Block Placement (`pipeline.py`, `parser/heuristic.py`, all 6 templates)
- **Section.start_page**: New field tracks which page each section heading appears on
- **Section.formula_blocks**: New field — formulas embedded within their source section
- **`_attach_formulas_to_sections()`**: Distributes FormulaBlocks by page-range proximity
- **Template update**: All 6 templates render section-level formula_blocks inline (after figures)
- **Fallback**: Unplaced formulas still go to "Key Equations" section at end of document
- **Renderer**: `_section_to_dict()` now includes `formula_blocks` per section

### Figure Caption Detection (`extractor/pdf_extractor.py`)
- **`_detect_figure_captions()`**: Scans text blocks for "Fig." / "Figure" patterns
- **Page-proximity matching**: Captions matched to figures on the same page
- **Caption parsing**: Strips "Fig. N." prefix, extracts clean caption text
- **Dedup**: Each caption block used at most once (prevents double-matching)

## Recent Changes (2026-03-25 — session 2)

### Author Extraction Fix (`parser/heuristic.py`)
- **Root cause**: `in_author_zone` only triggered on large-font title blocks. PDFs with same-size bold titles (common in word-processor papers) never opened the zone → 0 authors extracted.
- **Fix 1**: Explicit "Authors"/"Author" heading block now triggers `in_author_zone = True`
- **Fix 2**: Affiliation lines (dept/org) and email lines now attached to the preceding Author object (was silently discarded)
- **Fix 3**: Trailing country in institution names parsed: "ABC University, India" → org="ABC University", country="India"
- **New helper**: `_attach_affiliation_line(author, text)` fills department → organization → country in order
- **Block window**: Extended from 20 → 35 blocks (structured layout has more blocks before abstract)

## Recent Changes (2026-03-25)

### Image & Formula Extraction Overhaul
- **`get_images()` as primary**: Replaced block-level image iteration with `page.get_images(full=True)`
- **Image classification**: `_classify_image()` uses size/aspect ratio to sort into equation vs figure vs skip
- **OCR-or-save fallback**: `_process_equation_image()` tries pix2tex → nougat → saves raw image
- **FormulaBlock.image_path**: New field — templates render equation images via `\includegraphics` when OCR unavailable
- **adjustbox package**: Added to all 6 templates for `max width`/`max height` on equation images

### Table Deduplication
- **Table bbox filtering**: pdfplumber table bboxes extracted first, fitz text blocks overlapping ≥50% are skipped
- **Table cell math cleanup**: Cells now processed with `_clean_table_cell()` (was `_clean()` only)
- **Table formula patterns**: Added regex patterns for common text-based formulas (a2+b2=c2 → $a^{2}+b^{2}=c^{2}$)

### Figure Handling
- **Path normalization**: Absolute paths → relative from intermediate/ for pdflatex
- **Forward slashes**: All image paths normalized for cross-platform LaTeX compatibility

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
1. PyMuPDF is primary extractor; pdfplumber only for tables + table bboxes
2. Canon gate is mandatory before rendering
3. ML OCR must run in subprocesses (segfault protection)
4. OCR output must be sanitized before LaTeX
5. pix2tex confidence < 0.45 → reject OCR, keep image fallback
6. One change at a time
7. Table bboxes from pdfplumber used to filter overlapping fitz text blocks
8. Image classification: equation (< 120pt tall) vs figure (≥ 100pt, area ≥ 15000)
9. All formula images saved to intermediate/figures/ regardless of OCR success

## Tasks Completed
- [x] Author extraction: structured "Authors" heading + multi-line block splitting (dept/org/email)
- [x] Table-in-text deduplication via bbox overlap detection
- [x] Image extraction via get_images() (replaces block-level iteration)
- [x] Image classification: equation vs figure vs skip
- [x] OCR-or-save fallback for equation images
- [x] FormulaBlock.image_path field + template rendering
- [x] Figure path normalization (absolute → relative for pdflatex)
- [x] Table cell math: all 8 formula patterns (Pythagoras, log, derivative, gravity, complex, relativity, etc.)
- [x] adjustbox package added to all 6 templates
- [x] Black-box image fix: pixmap rendering instead of raw extract_image() for equations
- [x] Unicode normalization in table cells (² → 2, − → -) before pattern matching
- [x] Adjacent math merging: $a^{2}$ + $b^{2}$ → $a^{2} + b^{2}$ (single equation)
- [x] Embed formula blocks at source location (by page proximity to sections)
- [x] Figure caption detection from nearby text blocks

## Tasks Pending
- [ ] Test on all input PDFs to verify no regressions (run on Windows with full dependencies)
- [ ] Multi-column layout: improve hyphenation artifact handling
- [ ] Reference cleaning: reduce in-text citation noise
