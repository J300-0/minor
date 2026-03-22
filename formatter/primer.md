# primer.md — ai-paper-formatter Task Tracker
> Updated every time a change is made. Read this to understand current status instantly.
> Last updated: 2026-03-22

---

## Current Status

**Pipeline**: working end-to-end (Extract → Parse → Normalize → Canon → Render → Compile)
**Test paper**: FedBNR (36-page Springer federated learning paper)
**Active focus**: pix2tex integration for heavy math formulas

---

## Task Board

### ✅ Done
| Task | Notes |
|------|-------|
| 5-stage pipeline running end-to-end | Produces IEEE PDF from Springer input |
| Font-aware + text-only parsing | 54→20 sections, 0→91 refs on FedBNR |
| Canon gate (Stage 2.5) | Validates + repairs Document before render |
| Math symbol normalizer | Handles Greek letters, operators, sub/superscripts |
| Rotating + latest log | `logs/pipeline.log` and `logs/pipeline_latest.log` |
| Multi-template support | IEEE, ACM, Springer, Elsevier, APA, arXiv |
| Image extraction (figures) | pdfplumber crop → PNG |

---

### 🔨 In Progress (Current Session — 2026-03-22)

**Task: pix2tex integration for heavy LaTeX math**

**Problem**: The text extraction pipeline (PyMuPDF → normalizer) can handle simple
symbols but fails on multi-level integrals, fractions, matrices, and aligned equations.
These get garbled as fragmented `$\alpha$ $\beta$` strings or dropped entirely.

**Root Cause Chain** (all 6 flaws fixed by the new math_extractor module):

| # | Flaw | Where | Fix |
|---|------|-------|-----|
| 1 | Math regions never isolated as images | `pdf_extractor.py` | Call `math_extractor.extract_formula_blocks()` |
| 2 | No math-region detector exists | (missing module) | New `extractor/math_extractor.py` |
| 3 | PyMuPDF extracts formula text as characters, not images | `pdf_extractor.py` | Use `page.get_pixmap(clip=bbox)` for formula crops |
| 4 | Normalizer produces fragile `$\alpha$$\beta$` fragments | `normalizer/cleaner.py` | pix2tex bypasses this entirely |
| 5 | No pipeline hook for pix2tex | `core/pipeline.py` | Add `formula_blocks` to rich dict → Document |
| 6 | `Figure` model only, no `FormulaBlock` model | `core/models.py` | Add `FormulaBlock` dataclass + `Document.formula_blocks` |

**Files to change (apply in this order)**:

1. `core/models.py` — add `FormulaBlock` dataclass, add `formula_blocks` to `Document`
2. `extractor/math_extractor.py` — **NEW FILE** (provided: `math_extractor.py`)
3. `extractor/pdf_extractor.py` — import + call `extract_formula_blocks()`, add to return dict
4. `core/pipeline.py` — attach `formula_blocks` to `Document` after `_parse()`
5. `renderer/jinja_renderer.py` — pass `formula_blocks` to template context
6. `template/ieee/template.tex.j2` (and other templates) — render equation blocks

**Install requirement**: `pip install pix2tex` (or `pip install pix2tex[gui]`)
Pipeline degrades gracefully to [] if pix2tex is absent.

---

### 📋 Up Next (Queued)

| Priority | Task | Why |
|----------|------|-----|
| HIGH | Test pix2tex on FedBNR paper — check confidence scores | Verify the 6-flaw fix actually works |
| HIGH | Embed formulas at their source location in the document (by page proximity) | Currently all formulas go to a `Key Equations` block at the end |
| MEDIUM | Fix ACM .cls name mismatch (`acmart.cls` vs `acmart-tagged.cls` in config) | ACM template silently fails |
| MEDIUM | Add `\subsection{}` support in templates | Only `\section{}` exists now |
| MEDIUM | Improve table-to-section attachment (use page numbers) | Currently keyword matching, unreliable |
| LOW | Add unit tests per stage | Prevents regressions |
| LOW | Add `--verbose` flag for console debug | Currently all debug goes to log file only |

---

## Known Bugs (unresolved)

| Bug | Symptom | File | How to Debug |
|-----|---------|------|--------------|
| Overfull hbox | Wide tables overflow column width in PDF | `renderer/jinja_renderer.py` | Check `intermediate/generated.log` for `Overfull \hbox` |
| ACM cls mismatch | ACM template fails silently | `core/config.py` | Config says `acmart.cls`, disk has `acmart-tagged.cls` |
| Hyperref bookmark warnings | Section numbering mismatch in PDF bookmarks | `template/ieee/template.tex.j2` | Check pdflatex log for `\hyperref` warnings |
| Unnumbered headings not detected | Springer LNCS sections break author extraction | `parser/heuristic.py` | `_HEADING_RE` only matches `N Title` / `II. Title` format |
| Display math `\[...\]` crashes pdflatex | "Display math should end with $$" | `normalizer/cleaner.py` | Search generated.tex for `\[` |

---

## How to Debug a Run

```
1. Run:   python main.py --input input/paper.pdf --template ieee
2. Check: logs/pipeline_latest.log       ← full stage-by-stage log
3. Check: intermediate/extracted.txt     ← raw extraction output
4. Check: intermediate/structured.json  ← parsed Document as JSON
5. Check: intermediate/generated.tex    ← LaTeX source before compilation
6. Check: intermediate/generated.log    ← pdflatex error transcript
```

---

## Project Structure (quick reference)

```
formatter/
  main.py                          CLI entry point
  core/
    config.py                      Paths, constants, template registry
    models.py                      Document, Author, Section, Table, Figure,
                                   FormulaBlock ← NEW, Reference
    pipeline.py                    Orchestrator (5 stages + canon gate)
    logger.py                      Logging setup
  extractor/
    pdf_extractor.py               PyMuPDF primary, pdfplumber tables only
    math_extractor.py              ← NEW: pix2tex formula region detection
    docx_extractor.py              python-docx
  parser/
    heuristic.py                   Font-aware + text-only section detection
  normalizer/
    cleaner.py                     Unicode / ligature / math symbol cleanup
  canon/
    builder.py                     Validate + repair + score Document
    models.py                      CanonicalDocument + FieldResult
  renderer/
    jinja_renderer.py              Jinja2 → LaTeX
  compiler/
    latex_compiler.py              pdflatex 2-pass
  template/
    ieee/  acm/  springer/  elsevier/  apa/  arxiv/
  input/  intermediate/  output/  logs/
```

---

## Key Rules (never break these)

1. **PyMuPDF is primary extractor.** pdfplumber = tables only. Springer PDFs CID-encode text; pdfplumber silently returns garbage.
2. **Canon gate is mandatory.** Never call the renderer on a Document that hasn't passed `build_canonical()`.
3. **Use inline math `$...$` only.** Display math `\[...\]` requires exact paragraph placement that Jinja2 templating cannot guarantee.
4. **pix2tex confidence filter.** Discard FormulaBlocks with `confidence < 0.45`. Bad OCR is worse than no formula.
5. **One change at a time.** The LLM + multi-template simultaneous expansion broke the codebase. Introduce changes one at a time and test after each.

---

## Quick Command Reference

```bash
# Run pipeline
python main.py --input input/paper.pdf --template ieee

# Install pix2tex
pip install pix2tex

# Check last run log
cat logs/pipeline_latest.log

# Check structured parse output
cat intermediate/structured.json | python -m json.tool | head -100
```