# CLAUDE.md — ai-paper-formatter Project Memory
> Read this file at the start of every session. Update it when structural changes are made.
> Last updated: 2026-03-20 — Added Canonical Structure Builder (Stage 2.5)

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
1. Extract   →  2. Parse   →  3. Normalize  →  2.5. Canon  →  4. Render  →  5. Compile
PDF/DOCX         raw text       fix unicode       validate +     Jinja2 →      pdflatex
→ raw text    →  → Document  →  math symbols  →  repair doc  →  .tex file  →  .pdf file
```

### Stage 1: Extract (`extractor/`)
- `pdf_extractor.py` — **PyMuPDF (fitz) is primary**. pdfplumber for tables only.
- `docx_extractor.py` — python-docx
- **Critical**: PyMuPDF handles CID/Adobe-Identity-UCS encoded PDFs (Springer). pdfplumber silently crashes on these → only use it for tables.

### Stage 2: Parse (`parser/`)
- `heuristic.py` — font-aware + text-only heading detection
- Detects: title, authors, abstract, keywords, sections, references
- Uses `_is_metadata_line()` to filter page headers, DOIs, copyright lines
- Uses `_is_heading_line()` to detect section boundaries

### Stage 3: Normalize (`normalizer/`)
- `cleaner.py` — pure local transforms, no external calls
- Fixes: ligatures (fi/fl/ff), unicode quotes/dashes, math symbols → LaTeX commands
- Greek letters, math operators, arrows, sub/superscripts

### Stage 2.5: Canonical Builder (`canon/`) ← NEW
See full section below.

### Stage 4: Render (`renderer/`)
- `jinja_renderer.py` — Jinja2 with LaTeX-safe delimiters (`\VAR{}`, `\BLOCK{}`)
- Custom filters: `latex_escape`, `latex_paragraphs`, `render_table`
- Copies required .cls files alongside .tex
- **Only receives documents that passed canon check**

### Stage 5: Compile (`compiler/`)
- `latex_compiler.py` — pdflatex 2-pass (for cross-references)
- Flags: `-interaction=nonstopmode -halt-on-error`
- Outputs `generated_{template}.pdf`

---

## Stage 2.5 — Canonical Structure Builder (NEW)

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
    pipeline.py              # Orchestrator — 5 stages + canon gate
    logger.py                # Rotating + latest log
  extractor/
    pdf_extractor.py         # PyMuPDF primary, pdfplumber tables only
    docx_extractor.py        # python-docx
  parser/
    heuristic.py             # Font-aware + text-only heading/section detection
  normalizer/
    cleaner.py               # Unicode cleanup, ligatures, math → LaTeX
  canon/                     # ← NEW Stage 2.5
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

---

## Dependencies

```
pymupdf>=1.23.0      # import as fitz — PDF extraction
pdfplumber>=0.10.0   # tables only
python-docx>=1.1.0   # DOCX extraction
jinja2>=3.1.0        # template rendering
requests>=2.31.0     # optional, future API use
scikit-learn         # optional, for ML classifier (canon/classifier.py)
```
**System:** `pdflatex` (TeX Live / MiKTeX) must be on PATH.

---

## Known Issues (as of 2026-03-20)

1. **Overfull hbox** — wide tables overflow column width despite `\resizebox` for >5 columns
2. **ACM .cls file** — needs verification that acmart-tagged.cls is correct version
3. **Multi-column extraction** — hyphenation artifacts from column layout in double-column PDFs
4. **Reference cleaning** — incomplete; some entries still contain in-text citation noise

---

## Changelog

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