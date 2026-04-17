# ADR-001: Bug Audit & Stabilization — ai-paper-formatter

**Status:** Accepted
**Date:** 2026-04-07
**Deciders:** Project maintainers

## Context

The ai-paper-formatter is a 6-stage pipeline (Extract → Parse → Canon → Normalize → Render → Compile) that reformats research papers into clean LaTeX PDFs. After rapid feature development (equation OCR, formula numbering, subscript extraction, table cell images), the codebase accumulated latent bugs that would crash on edge-case inputs even though the happy path worked.

A comprehensive audit was performed covering all 13 Python modules (~5,500 lines of code). The pipeline was tested against all 6 templates (IEEE, ACM, Springer, Elsevier, APA, arXiv).

## Decision

Fix all identified bugs and document the architectural state for future development.

## Bugs Found and Fixed

### Critical (3 fixed)

| Location | Bug | Fix |
|----------|-----|-----|
| `canon/builder.py:138` | `.lower()` called on potentially None `s.heading` — crashes with `AttributeError` | Added `s.heading and` guard before `.lower()` call |
| `canon/builder.py:199` | `_is_metadata_heading()` calls `.lower()` without None check | Added early `if not heading: return False` |
| `renderer/jinja_renderer.py:219` | `remaining.find(eq_end)` searched from position 0 instead of after `eq_start` — could match `\end{equation*}` before `\begin{equation*}` | Changed to `remaining.find(eq_end, idx_start + len(eq_start))` |

### High (4 fixed)

| Location | Bug | Fix |
|----------|-----|-----|
| `renderer/jinja_renderer.py:250` | `isinstance(f, str)` check on Figure object — impossible condition, wrong fallback path | Replaced with `getattr(f, "image_path", ...)` pattern |
| `parser/heuristic.py:537` | `stripped[0].isupper()` without empty-string guard | Added `stripped and` prefix |
| `extractor/docx_extractor.py:46` | `para.style.name[-1]` without None/empty check | Added `para.style and para.style.name and` guards |
| `core/pipeline.py:96` | `doc.sections[-1]` IndexError when sections list is empty | Added None-safe fallback chain with guard |

### Medium (2 fixed)

| Location | Bug | Fix |
|----------|-----|-----|
| `canon/builder.py:185` | `s.body.strip()` without None check — crashes if body is None | Changed to `not s.body or not s.body.strip()` |
| `canon/models.py:43` | `s.body.strip()` in `is_renderable()` without None guard | Added `s.body and` prefix |

### Session-Specific Fixes (same session, before audit)

| Location | Fix |
|----------|-----|
| `extractor/pdf_extractor.py` | Rewrote `_build_blocks_from_chars()` to detect subscript/superscript characters by font size and merge them into parent lines as `$base_{sub}$` math expressions |
| `extractor/pdf_extractor.py` | Rewrote `_match_eq_nums_from_text_blocks()` to use formula center-y (not top), scale tolerance by formula height, and detect inline trailing `(N)` patterns |
| `core/models.py` | Added `bbox_h: float` field to `FormulaBlock` for height-aware equation number matching |
| `normalizer/cleaner.py` | Added pre-pass to convert Greek/math symbols inside existing `$...$` without double-wrapping — prevents nested `$$` errors |
| `normalizer/cleaner.py` | Strip `$...$` from math body before inserting into `equation*` environment |

## Verification

All 6 templates compile successfully with zero LaTeX errors:

| Template | Status | Output Size | Time |
|----------|--------|-------------|------|
| IEEE | Pass | 222 KB | 7.7s |
| ACM | Pass | 170 KB | 7.8s |
| Springer | Pass | 244 KB | 7.7s |
| Elsevier | Pass | 233 KB | 10.8s |
| APA | Pass | 180 KB | 5.9s |
| arXiv | Pass | 219 KB | 9.2s |

All 13 Python modules import cleanly. Zero `pdflatex` errors in compilation logs.

## Remaining Technical Debt

### High Priority
- **No automated tests.** The pipeline has zero unit tests. Each bug fix risks regressions. A test suite covering extraction, parsing, normalization, and rendering would catch issues before they reach the user.
- **PyMuPDF unavailable in Linux sandbox.** The primary extractor (fitz) doesn't work here — all extraction falls back to pdfplumber. The PyMuPDF code path is untested in this environment.
- **OCR engines unavailable.** pix2tex and nougat can't run (Windows executables on Linux). All equations render as images, never as LaTeX. The OCR code paths are untested.

### Medium Priority
- **Magic numbers scattered across codebase.** Thresholds like `MIN_MATH_CHARS=5`, `OCR_CONFIDENCE_THRESHOLD=0.60`, `max_y_dist=80.0` are defined in-place rather than in `shared.py` or `config.py`.
- **Duplicated paragraph splitting.** `r"\n\n+"` regex used in multiple places in the renderer. Should be a shared helper.
- **Font-based heading detection fragile.** `body_size * 1.15` threshold works for most papers but fails on papers with unusual font hierarchies.
- **Reference extraction incomplete.** Author-year citation style (Harvard) only partially supported. Numbered references (`[1]`, `1.`) work well.

### Low Priority
- **Dead code in `_classify_image`.** The skip-small-image check (`h < 15 or w < 15`) overlaps with the equation height check. Could be simplified.
- **Inconsistent label generation.** Some use `len(list)+1`, others use a dedicated counter variable. Should standardize on counter.
- **`_OcrBudget` global state.** While it has `reset()`, the global pattern makes testing harder. Consider dependency injection.

## Architecture Assessment

The 6-stage pipeline architecture is sound. The canon gate (Stage 3) prevents bad data from reaching the renderer — this is the most important architectural decision and should be preserved. Key strengths: graceful degradation (PyMuPDF → pdfplumber, pix2tex → nougat → Tesseract → image fallback), subprocess isolation for ML models, and template isolation via Jinja2.

The main architectural risk is the normalizer's complexity (~1,600 lines with many regex passes). Each new pattern risks interacting with existing ones. The subscript/Greek/math-mode nesting issue fixed in this session is a symptom of this complexity. Consider splitting the normalizer into focused passes with clear input/output contracts.

## Action Items

1. [x] Fix all critical and high severity bugs
2. [x] Fix medium severity bugs
3. [x] Verify all 6 templates compile cleanly
4. [x] Document fixes in this ADR
5. [ ] Add unit tests for extraction, parsing, and normalization
6. [ ] Consolidate magic numbers into `config.py`
7. [ ] Split normalizer into focused passes
