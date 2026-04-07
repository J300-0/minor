# Code Review: ai-paper-formatter

## Summary

The codebase is a well-architected 6-stage pipeline with clear separation of concerns. The CLAUDE.md is exceptional documentation. However, the project has accumulated significant technical debt from rapid iterative fixes: three 1,500+ line files (pdf_extractor, heuristic, cleaner), pervasive code duplication, mutable global state, and a type system that fights itself (dict vs dataclass). Below are findings organized by severity.

---

## Critical Issues

| # | File | Issue | Severity |
|---|------|-------|----------|
| 1 | `pdf_extractor.py` | `MATH_CHARS` set redefined **3 separate times** (lines 69-74, cleaner.py:150-153, cleaner.py:415-417) with **different contents** each time. The extractor version includes Unicode ranges, the cleaner versions are hardcoded subsets missing many characters. Behavior diverges silently. | :red_circle: Critical |
| 2 | `pipeline.py:64-98` | Formula blocks are **both** `dict` and `FormulaBlock` — the pipeline does `isinstance(fb, dict)` checks everywhere. This means a typo in a dict key silently produces `0`/`""` via `.get()` instead of crashing. Data flows as dicts from extractor, but `FormulaBlock` from deserialization — no single source of truth. | :red_circle: Critical |
| 3 | `pdf_extractor.py:49` | `_ocr_budget` is a **mutable global dict** shared across all calls. If the module is imported by tests or a web server, the budget state leaks between invocations. `set_ocr_budget()` resets `start` but not `exhausted` properly for re-entrant use. | :red_circle: Critical |
| 4 | `normalizer/cleaner.py:341` | Operator precedence bug: `"..." in line and ("k(" in line or "||" in line)` — the `"..." in line` binds with the `or` to its left (`"· · ·" in line`), not the `and` to its right. Should be: `("..." in line and ("k(" in line or "||" in line))`. | :red_circle: Critical |
| 5 | `renderer/jinja_renderer.py:340` | `$` math-mode tracking is fragile — `$$` (display math) is treated as two toggles that cancel out, but `$$ x $$` would leave `math_depth=0` mid-content. Escaped `\$` is handled but `$$` display math is not. Any section body containing `$$...$$` will have its content incorrectly escaped. | :orange_circle: High |
| 6 | `pdf_extractor.py:302` | `dist = abs(cy - 0)` — the figure's y-position is hardcoded to `0` instead of using the figure's actual bbox_y. Every caption "distance" calculation degenerates to just `abs(cy)`, biasing toward captions at the top of the page regardless of where the figure is. | :orange_circle: High |
| 7 | `compiler/latex_compiler.py:57-94` | If pass 1 produces a PDF but with errors, and pass 2 **also** fails, the error from pass 2 overwrites pass 1's log. No distinction between "pass 1 errors" vs "pass 2 errors" in the log file. On pass 2 fatal failure, the code falls through to the `shutil.copy2` which may copy a corrupt PDF silently. | :orange_circle: High |
| 8 | `pdf_extractor.py:748-753` | Figure label uses `len(figures)` at time of creation: `f"fig_{page_num}_{len(figures)}"`. But the label is generated BEFORE the `figures.append()`, so two consecutive figures get the same counter value if processing is interleaved — label collision on the same page. | :orange_circle: High |

---

## Code Duplication (Repeated Patterns)

### 1. `_attach_formulas_to_sections` and `_attach_figures_to_sections` — near-identical

**pipeline.py:151-274**: These two functions implement the *exact same* page-range proximity algorithm, duplicated across 120 lines. The only differences are the attribute names (`formula_blocks` vs `figures`) and the sort key.

**Fix**: Extract a generic `_distribute_items_to_sections(doc, items, attr_name, sort_key)` function.

### 2. "Real words" filter — repeated 5+ times

**cleaner.py:184, 360, 370, 490, 862**: The same list comprehension pattern:
```python
real_words = [w for w in words if w.isalpha() and len(w) >= 4
              and w.lower() not in ("true", "false", ...)]
```
Each instance has a **different** exclusion set (some include "prior", "posterior", "prediction"; others don't). This means the same text is classified differently depending on which function processes it.

**Fix**: Create `_count_real_words(words, extra_exclude=None)` that uses a single canonical stop-word set.

### 3. PIL import + io.BytesIO + composite-on-white — repeated 7 times

**pdf_extractor.py:445-475, 512-528, 535-548, 576-582**: `from PIL import Image; import io` is imported inside functions 7 times. The "open image, check mode, composite on white, save to BytesIO" pattern is repeated in `_composite_on_white`, `_composite_with_smask` (twice), and `_process_equation_image`.

**Fix**: Import PIL/io once at module level (guarded by try/except). Consolidate the composite logic into `_composite_on_white` and use it everywhere.

### 4. Image save + classification + dict construction — repeated 3 times

**pdf_extractor.py:720-758, 780-798**: The "classify → process equation / save figure → build dict" pattern is copy-pasted for xref images, inline images, and pdfplumber images. Each copy has slightly different dict keys and file naming.

**Fix**: Extract `_save_figure(img_bytes, page_num, counter, fig_dir) -> dict` and `_save_equation(img_bytes, page_num, counter, fig_dir) -> dict`.

### 5. OCR confidence check — threshold `0.60` hardcoded in 6 places

**pdf_extractor.py:938, 1039, 1060** and **pipeline.py:72**: The confidence threshold `0.60` is scattered as a magic number. If you change it in one place and miss another, you get inconsistent filtering.

**Fix**: Define `OCR_CONFIDENCE_THRESHOLD = 0.60` once in `core/config.py`.

### 6. `_is_garbled_math_line` and `_is_equation_fragment` — overlapping logic

**cleaner.py:325-394 vs 460-499**: Both functions detect "garbage math text" using nearly identical character analysis (math char counting, real-word filtering, line length checks). They just have slightly different thresholds.

**Fix**: Merge into a single `_is_math_garbage(line, mode='block'|'fragment')` with configurable thresholds.

### 7. Path relativization + backslash normalization

**jinja_renderer.py:228-234, 252-257, 596-602, 612-618**: The "convert abs path to relative, replace `\\` with `/`" pattern is repeated 4 times.

**Fix**: `_latex_relpath(abs_path)` utility.

---

## Scalability Issues

### 1. `pdf_extractor.py` is 2,000+ lines — single-file monolith

This file handles: image extraction, image classification, alpha compositing, OCR orchestration, batch OCR, table extraction, formula detection, caption matching, cell image rendering, space recovery, and pdfplumber fallback. It's nearly impossible to test individual components.

**Fix**: Split into modules: `extractor/images.py`, `extractor/ocr.py`, `extractor/tables.py`, `extractor/compositing.py`.

### 2. No parallelism in page processing

**pdf_extractor.py:131-201**: Pages are processed sequentially in a for loop. For a 50-page paper, this means 50 sequential rounds of image extraction, classification, and OCR. Since each page's processing is independent, this could use `concurrent.futures.ThreadPoolExecutor`.

### 3. Entire PDF held in memory

**pdf_extractor.py:121**: `fitz.open(path)` loads the entire PDF. For very large documents (100+ pages with many figures), this can consume significant memory. Pages could be processed and released incrementally.

### 4. Normalizer applies 12 regex passes over every section body

**cleaner.py:660-683**: Each section body goes through `_remove_running_headers`, `_remove_charperline_garbage`, `_remove_repeated_table_captions`, `_merge_orphan_symbol_lines`, `_remove_garbled_math_blocks`, `_remove_fragmented_equations`, and `_clean_with_math` (which itself has 12 sub-steps). For a paper with 20 sections, that's 240+ regex passes over text. Several of these split and rejoin the text on `\n` repeatedly.

**Fix**: Combine the line-by-line passes into a single scan where possible.

### 5. pdfplumber opened twice

**pdf_extractor.py:119, 212**: `_get_table_bboxes(path)` opens pdfplumber to get bboxes, then `_extract_tables_pdfplumber(path)` opens it again for full table extraction. Two full PDF parses for the same data.

**Fix**: Extract tables and bboxes in a single pdfplumber pass.

### 6. Template system doesn't support custom templates

**core/config.py**: `TEMPLATE_REGISTRY` is a hardcoded dict. Adding a new template requires modifying source code. No way to pass a custom template directory at runtime.

**Fix**: Allow `--template-dir` CLI arg that registers custom templates dynamically.

---

## Bugs Likely to be Encountered

### 1. Windows path separator in LaTeX

**jinja_renderer.py**: While `_figure_to_dict` normalizes `\` to `/`, the `_escape_table_cell` function's `\CELLIMG{path}` uses the raw path from extraction. On Windows, this produces `\CELLIMG{C:\Users\...}` which LaTeX interprets as commands (`\U`, `\f`, etc.).

### 2. Race condition in temp file cleanup

**pdf_extractor.py:999-1006**: The retry loop for `os.unlink(tmp_path)` sleeps 0.1s and retries 3 times — but if the OCR subprocess is still running (e.g., nougat is slow), the file may be locked for longer. The `finally` block could silently fail, leaking temp files.

### 3. `fig_counter` never used

**pdf_extractor.py:127, 203**: `fig_counter` is initialized to 0, then set to `len(all_figures)` at line 203, but is never actually used anywhere. Dead variable.

### 4. Empty `formula_blocks` list on Section dataclass is shared

**core/models.py:61**: `formula_blocks: List['FormulaBlock'] = field(default_factory=list)` is correct. BUT — the `_attach_formulas_to_sections` function appends to `section.formula_blocks` which means sections that were never given formulas share the same empty list reference — wait, `default_factory=list` creates new lists. This is fine. However, `Document.formula_blocks` at line 73 is set to `[]` at pipeline.py:98 after distribution, but if canon rebuilds the document via `to_document()`, those formula_blocks may be lost.

### 5. Caption matching ignores figure y-position entirely

**pdf_extractor.py:302**: As noted in Critical Issues, `dist = abs(cy - 0)` means the figure's actual position on the page is ignored. A figure at the bottom of the page will match a caption at the top rather than the one directly below it.

### 6. `_score_ocr_quality` not applied to text-region OCR in pdfplumber path

The pdfplumber extraction path (`_extract_with_pdfplumber`) calls `_batch_ocr_equations` which does apply quality scoring. But if batch OCR fails and it falls back to single workers, the confidence is set to the raw OCR output without the scoring heuristic for the pdfplumber code path specifically.

### 7. `_clean_with_math` double-wraps Greek in already-math context

**cleaner.py:751-753**: Greek letter replacement does `text.replace(char, f"${cmd}$")` globally. If the text already contains `$\alpha + \beta$`, and there's a stray `α` elsewhere, the replacement works. But if the α is INSIDE an existing `$...$` block, it becomes `$...$\alpha$...$` — broken math mode nesting. The function has no awareness of existing math-mode regions.

### 8. Infinite loop risk in `_remove_charperline_garbage`

**cleaner.py:520-537**: When `run_len == 0` (the line is longer than 3 chars), the code falls through to:
```python
if i == run_start:
    result.append(lines[i])
    i += 1
```
This is correct. But if a line is exactly 3 chars (e.g., `"abc"`), it enters the while loop, increments `i`, then `run_len = 1`, which is `< 4`, so lines are kept. However, if ALL lines are <=3 chars, the outer while loop's `i` advances correctly. No actual infinite loop — but the logic is confusing and brittle.

---

## What Looks Good

- **CLAUDE.md** is one of the best project memory documents I've seen. The technical rules section prevents regression of past bugs.
- **Subprocess isolation** for OCR is a smart architectural choice that handles CUDA segfaults gracefully.
- **Canon gate** (validation + repair before rendering) is excellent defensive programming. The confidence scoring and repair logging make debugging much easier.
- **OCR-or-save pattern** — always saving the equation image as fallback when OCR fails means no data is ever lost.
- **Batch OCR** (loading pix2tex once for all images) is a significant performance optimization.
- **Template isolation** — each template's Jinja2 file handles its own formatting, keeping shared code template-agnostic.
- **Comprehensive logging** — the rotating + latest-log pattern with debug-level detail makes post-mortem analysis feasible.

---

## Verdict: Request Changes

The codebase works and has sophisticated domain logic, but the three mega-files need decomposition, the duplicated patterns need consolidation into utilities, and the dict/dataclass inconsistency needs resolution before the next feature addition. The operator precedence bug in the garbled-math detector (Issue #4) and the broken caption matching (Issue #6) should be fixed immediately.

**Priority order:**
1. Fix the bugs (issues #4, #6, #8)
2. Consolidate `MATH_CHARS` into a single shared definition
3. Unify dict/dataclass handling (always use dataclasses from extraction onward)
4. Extract the 5 duplicated patterns into utility functions
5. Split `pdf_extractor.py` into submodules
