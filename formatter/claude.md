# Paper Formatter - Project Context & Memory

> **Last updated:** 2026-03-18
> **Status:** Working MVP - pipeline runs end-to-end, produces PDF output
> **Role:** You are a senior automation dev building a robust, scalable research paper formatting tool.
> **Rule:** After every change, update this CLAUDE.md file. Read it before starting any work.

---

## What This Project Does

Takes semi-structured or unstructured academic papers (PDF/DOCX) and reformats them into properly structured LaTeX-compiled PDFs in standard academic formats (IEEE, ACM, Springer, Elsevier, APA, arXiv).

**CLI usage:**
```
python main.py input/paper.pdf
python main.py input/paper.pdf --template acm
python main.py input/paper.docx --template springer --output my_output/
```

---

## Architecture - 5-Stage Pipeline

```
Input (PDF/DOCX) -> Extract -> Parse -> Normalize -> Render (.tex) -> Compile (.pdf)
```

### Stage 1: Extract (`extractor/`)
- `pdf_extractor.py` - Uses pymupdf (fitz) for font-aware text blocks + pdfplumber for tables
- `docx_extractor.py` - Uses python-docx for paragraphs + tables
- Output: `{ raw_text, blocks[{text, font_size, bold, page}], tables, images }`
- Fallback: if pymupdf unavailable, pdfplumber plain text extraction (no font info)

### Stage 2: Parse (`parser/`)
- `heuristic.py` - Two modes:
  - **Font-aware (Mode A):** Uses font_size median + threshold to detect headings, bold detection, title = largest font on page 1
  - **Text-only (Mode B):** Regex-based heading detection (`1. Title`, `II. Title`, ALL CAPS), sequential line walking
- Detects: title, authors (name/dept/org/city/email), abstract, keywords, sections, tables, references
- Known section names in `_SECTION_NAMES` set
- Reference extraction: splits on `[N]` markers after "References" heading

### Stage 3: Normalize (`normalizer/`)
- `cleaner.py` - Pure local transforms, no external calls
- Fixes: ligatures (fi/fl/ff), unicode quotes/dashes, math symbols -> LaTeX commands
- Greek letters, math operators, arrows, set theory symbols, sub/superscripts
- Safety net: catches remaining non-ASCII that pdflatex can't handle, decomposes or drops

### Stage 4: Render (`renderer/`)
- `jinja_renderer.py` - Jinja2 with LaTeX-safe delimiters (`\VAR{}`, `\BLOCK{}`)
- Custom filters: `latex_escape` (preserves $math$), `latex_paragraphs`, `render_table`
- Copies required .cls files alongside .tex
- Sanitizes all Document fields to non-None strings before rendering

### Stage 5: Compile (`compiler/`)
- `latex_compiler.py` - Runs pdflatex 2 passes (for cross-references)
- Flags: `-interaction=nonstopmode -halt-on-error`
- Copies .cls to work dir, outputs `generated_{template}.pdf`

---

## Data Model (`core/models.py`)

```
Document
  - title: str
  - authors: List[Author]  (name, department, organization, city, country, email)
  - abstract: str
  - keywords: List[str]
  - sections: List[Section]  (heading, body, tables: List[Table], figures: List[Figure])
  - references: List[Reference]  (index, text)
```

Supports JSON serialization (`to_json`, `from_json`, `to_dict`, `from_dict`).

---

## File Structure

```
formatter/
  main.py                   # CLI entry point
  requirements.txt          # pymupdf, pdfplumber, python-docx, jinja2, requests
  workflow.mermaid           # Visual pipeline diagram
  CLAUDE.md                 # THIS FILE - project memory, always read first
  core/
    config.py               # Paths, constants, template registry, pdflatex settings
    models.py               # Dataclasses: Document, Author, Section, Table, Figure, Reference
    pipeline.py             # Orchestrator: run() calls all 5 stages
    logger.py               # Rotating file + latest log, pipeline helpers
  extractor/
    pdf_extractor.py        # pymupdf + pdfplumber PDF extraction
    docx_extractor.py       # python-docx DOCX extraction
  parser/
    heuristic.py            # Font-aware + text-only heading/section detection
  normalizer/
    cleaner.py              # Unicode cleanup, ligatures, math symbol LaTeX conversion
  renderer/
    jinja_renderer.py       # Jinja2 -> LaTeX with escape filters
  compiler/
    latex_compiler.py       # pdflatex 2-pass compilation
  template/
    ieee/     (IEEEtran.cls + template.tex.j2)
    acm/      (acmart-tagged.cls + template.tex.j2)
    springer/ (llncs.cls + template.tex.j2)
    elsevier/ (elsarticle.cls + template.tex.j2)
    apa/      (template.tex.j2)
    arxiv/    (template.tex.j2)
  input/                    # Drop input PDFs/DOCX here
  intermediate/             # extracted.txt, structured.json, generated.tex, .cls copies
  output/                   # Final PDFs: generated_{template}.pdf
  logs/                     # pipeline.log (rotating), pipeline_latest.log (per-run)
```

---

## Templates Available

| Template  | .cls file         | Notes                    |
|-----------|-------------------|--------------------------|
| ieee      | IEEEtran.cls      | Conference format        |
| acm       | acmart-tagged.cls | ACM format               |
| springer  | llncs.cls         | LNCS format              |
| elsevier  | elsarticle.cls    | Elsevier journal format  |
| apa       | (none)            | APA style                |
| arxiv     | (none)            | arXiv preprint format    |

All templates use Jinja2 `.tex.j2` files with LaTeX-safe delimiters.

---

## Dependencies

- `pymupdf>=1.23.0` (import as `fitz`) - PDF text + font extraction
- `pdfplumber>=0.10.0` - PDF table extraction + fallback text
- `python-docx>=1.1.0` - DOCX extraction
- `jinja2>=3.1.0` - Template rendering
- `requests>=2.31.0` - (optional, for future API integrations)
- **System:** `pdflatex` (TeX Live / MiKTeX) must be on PATH

---

## Known Issues & Warnings (from latest run)

1. **Overfull hbox warning** - Table rendering at lines 61-75 produces `Overfull \hbox (49.83818pt too wide)` - wide tables overflow column width despite `\resizebox` for >5 columns
2. **pdfTeX dest warnings** - `name{section*.1}` and `name{section.13}` referenced but don't exist - likely from hyperref bookmarks on unnumbered sections
3. **~~References = 0 extracted~~ FIXED** - Now supports author-year style references (91 refs extracted from `your_paper.pdf`)
4. **Image extraction not implemented** - `images` always returns `[]`, Figure model exists but no extraction logic
5. **ACM .cls mismatch** - Config `CLS_FILES` says `acmart.cls` but template dir has `acmart-tagged.cls` - will fail if ACM template is selected
6. **Table data heuristic too aggressive** - `_is_table_data_line()` has hardcoded benchmark names (Livermore, Clinpack, SPEC) that are specific to one paper type

---

## Changes Log

### 2026-03-19 (v2) - Second round of parser fixes (appendix, keywords, junk sections)
- **Appendix stop**: Section collection now stops at `Appendix [A]:` pattern (colon-required). Prevents appendix content from leaking. Uses strict pattern to avoid false-positive on body sentences like "see Appendix A. We note..."
- **Keywords extraction**: Abstract now consumes multi-line continuation before reaching keywords line — fixes the multi-line abstract issue that caused keywords to always be `[]`
- **Sentence-fragment headings**: Numbered headings like `"2. We train pFedGP..."` rejected if heading text starts with sentence-starters (We, The, In, etc.)
- **Junk ALL CAPS sections**: `)MES(`, `ESMR`, `PPCC` no longer detected as headings — ALL CAPS check now requires only letters+spaces (`^[A-Z][A-Z\s]+$`)
- **CC BY license line**: `_is_metadata_line()` now catches Creative Commons lines; `"4.0 International (CC BY 4.0) license..."` no longer creates a spurious section
- **Publisher boilerplate in refs**: Filters "Springer Nature or its licensor...", "Authors and Affiliations", "Open Access" from reference list
- Result: 14 clean sections, 4 keywords correctly extracted, 82 clean refs on `your_paper.pdf`

### 2026-03-19 - Major text-only parser fixes for Springer journal papers
- **Title extraction**: No longer blindly takes first line; scans first 30 lines with scoring heuristic that penalizes author lists (commas, interpuncts, digits) and metadata lines
- **Author extraction**: Now handles superscript digits (`Yu1,2`), interpunct-separated author lists (`A · B · C`), and cleans author names
- **Page header filtering**: New `_is_metadata_line()` function catches journal headers like `"72 Page 2 of 36 Machine Learning (2026) 115:72"`, DOIs, copyright lines, Received/Accepted dates
- **Math fragment filtering**: `_is_heading_line()` now rejects lines with <3 alphabetic chars (prevents `"?N ·"` from becoming sections)
- **Reference extraction**: Added author-year citation support as fallback when `[N]` style not found (91 refs extracted from FedBNR paper)
- **Metadata skip in body**: Section parsing loop now filters metadata lines so page headers don't appear in section text
- Sections reduced from 54→20, references from 0→91 on `your_paper.pdf`

### 2026-03-18 - Initial project creation & MVP
- Full 5-stage pipeline implemented and working end-to-end
- 6 templates created (IEEE, ACM, Springer, Elsevier, APA, arXiv)
- Successfully generated `generated_ieee.pdf` and `generated_springer.pdf` from `your_paper.pdf`
- Font-aware and text-only parsing modes both functional
- Comprehensive normalizer handles ligatures, math symbols, Unicode edge cases
- Created this CLAUDE.md as project memory file

---

## Next Steps / Planned Improvements

### High Priority (Bugs)
- [ ] Fix ACM .cls filename mismatch (`acmart.cls` in config vs `acmart-tagged.cls` on disk)
- [ ] Fix hyperref bookmark warnings (section numbering mismatch)
- [x] Improve reference extraction to handle non-`[N]` citation styles (author-year, numbered without brackets)

### Medium Priority (Features)
- [ ] Implement image extraction from PDFs (extract embedded images, associate with figures)
- [ ] Add subsection support in templates (currently only `\section{}`, no `\subsection{}`)
- [ ] Improve table-to-section attachment heuristic (use page numbers instead of keyword matching)
- [ ] Add figure/image placement in LaTeX output
- [x] Support multiple authors on a single line (interpunct/and separated)

### Low Priority (Polish)
- [ ] Remove hardcoded benchmark names from `_is_table_data_line()`
- [ ] Add unit tests for each stage
- [ ] Add `--verbose` flag for console debug output
- [ ] Support `.doc` format (currently in SUPPORTED_EXTENSIONS but no extractor)
- [ ] Add progress bar / rich console output
- [ ] Consider LLM-assisted parsing for ambiguous structures

---

## How to Debug

1. Check `logs/pipeline_latest.log` for the most recent run
2. Check `intermediate/extracted.txt` for raw extraction output
3. Check `intermediate/structured.json` for parsed document model
4. Check `intermediate/generated.tex` for the LaTeX source before compilation
5. Check `intermediate/generated.log` for pdflatex compilation details

---

## Configuration Quick Reference (`core/config.py`)

- `DEFAULT_TEMPLATE = "ieee"`
- `SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}`
- `PDFLATEX_PASSES = 2`
- `PDFLATEX_FLAGS = ["-interaction=nonstopmode", "-halt-on-error"]`
- All paths are relative to project ROOT (auto-detected)