# CLAUDE.md — AI Paper Formatter: Memory Dump & Bug Report

> **Purpose**: Context dump so Claude (or any dev) can instantly understand the project,
> find the bugs, and fix them without re-reading every file.
>
> **Last updated**: 2026-03-18
> **Status**: Pipeline stalls/crashes between Stage 4 (Render) and Stage 5 (Compile)

---

## 1. PROJECT OVERVIEW

**What it does**: Takes a PDF or DOCX academic paper (semi-structured/unstructured input)
and reformats it into a properly structured LaTeX PDF in a target conference format
(IEEE, ACM, Springer, Elsevier, APA, arXiv).

**Entry point**: `python main.py input/paper.pdf --template ieee`

**5-Stage Pipeline** (`core/pipeline.py → run()`):

```
Stage 1: Extract    → PDF/DOCX → {raw_text, tables, images}
Stage 2: Parse      → raw text → Document dataclass (AI or heuristic)
Stage 3: Normalize  → fix ligatures, Unicode math → cleaned Document
Stage 4: Render     → Document → .tex via Jinja2 template
Stage 5: Compile    → .tex → PDF via pdflatex (2 passes)
```

---

## 2. DIRECTORY STRUCTURE (Active Code)

```
formatter/
├── main.py                        # CLI entry
├── core/
│   ├── config.py                  # All paths, constants, LM Studio settings
│   ├── models.py                  # Document, Section, Author, Table, Reference, Figure
│   ├── pipeline.py                # Orchestrator — calls all 5 stages
│   └── logger.py                  # Rotating log + per-run log
├── extractor/
│   ├── pdf_extractor.py           # PyMuPDF (text+images) + pdfplumber (tables)
│   └── docx_extractor.py          # python-docx
├── ai/
│   ├── structure_llm.py           # Stage 2A — LM Studio / Qwen AI parser
│   ├── heuristic_parser.py        # Stage 2B — regex-based fallback parser
│   └── cleaning_llm.py            # Stage 3  — ligature/unicode/math normalize
├── mapper/
│   └── base_mapper.py             # latex_escape, inject_tables (shared)
├── template/
│   └── renderer.py                # Jinja2 renderer (shared by all templates)
├── templates/
│   ├── ieee/template.tex.j2       # IEEE Jinja2 template
│   ├── acm/template.tex.j2
│   ├── springer/template.tex.j2
│   ├── elsevier/template.tex.j2
│   ├── apa/template.tex.j2
│   └── arxiv/template.tex.j2
├── compiler/
│   └── latex_compiler.py          # pdflatex wrapper (Stage 5)
├── intermediate/                  # Working files (extracted.txt, structured.json, generated.tex)
├── logs/                          # pipeline.log, pipeline_latest.log
└── check/
    └── structure.py               # Pre-flight validation script
```

### ⚠️ DEAD CODE — `stages/` folder

There is an **old `stages/` folder** that MUST be deleted. It shadows new imports:

| Old (stages/)                    | New (active)                  |
|----------------------------------|-------------------------------|
| stages/layout_parser.py         | extractor/pdf_extractor.py    |
| stages/document_parser.py       | ai/heuristic_parser.py        |
| stages/ai_structure_detector.py | ai/structure_llm.py           |
| stages/normalizer.py            | ai/cleaning_llm.py            |
| stages/template_renderer.py     | template/renderer.py          |
| stages/latex_compiler.py        | compiler/latex_compiler.py    |

`check/structure.py` explicitly checks for this and will raise `RuntimeError` if `stages/` exists.

---

## 3. DATA MODEL (`core/models.py`)

```python
Document
├── title: str
├── authors: list[Author]          # name, department, org, city, country, email
├── abstract: str
├── keywords: list[str]
├── sections: list[Section]
│   ├── heading: str
│   ├── body: str                  # may contain %%RAWTEX%%...%%ENDRAWTEX%% blocks
│   ├── tables: list[Table]        # caption, headers, rows, notes
│   └── figures: list[Figure]      # caption, image_path, label
└── references: list[Reference]    # index (int), text (str)
```

Key serialization methods:
- `doc.to_dict()` — uses `dataclasses.asdict()` (plain dicts, for JSON)
- `doc.to_dict_with_objects()` — returns actual dataclass objects (for Jinja2 attribute access like `ref.text`, `author.name`)
- `Document.from_dict(data)` / `Document.from_json(path)` — deserialize

---

## 4. KEY CONFIGURATION (`core/config.py`)

```python
LM_STUDIO_URL     = "http://localhost:1234/v1"
LM_STUDIO_MODEL   = "qwen3-8b"
LM_STUDIO_TIMEOUT = 60          # connect + first-token only
LM_BATCH_CHARS    = 10_000      # chars per LLM call
LM_MAX_TOKENS     = 8_192       # output cap per call
PDFLATEX_PASSES   = 2
PDFLATEX_FLAGS    = ["-interaction=nonstopmode"]
```

---

## 5. PIPELINE LOG ANALYSIS (from `pipeline_latest.log`)

### What happened in the latest run:

```
12:53:05  Stage 1 Extract  ✓  3749 chars, 0 tables, 0 images
12:53:05  Stage 2 Parse    — AI path
12:53:07  AI header start
12:56:55  AI header done   — 491 tokens, ~228s (!!!)
12:56:55  Header: title='An Example Conference Paper', 1 author
12:56:55  Batch 1/1 start  — 2949 chars
13:04:46  Batch 1/1 done   — 2166 tokens, 471.4s (!!!)
13:04:46  AI parse complete — 1 section, 0 tables, 5 refs
13:04:46  Stage 3 Normalize ✓
13:04:46  Stage 4 Renderer — Document → LaTeX [ieee]
          *** LOG ENDS HERE — no Stage 5, no RUN END ***
```

### Observations:

1. **LLM is extremely slow** — 228s for header, 471s for body batch. Qwen3-8b on this hardware is struggling. This isn't a bug per se, but a performance issue.

2. **Only 1 section detected** — the AI returned only 1 section from the entire body. This means the Jinja2 template will produce a nearly-empty document with one massive section blob. The prompt or the model isn't splitting sections properly.

3. **0 tables extracted** — pdfplumber found no ruled-line tables. Expected for some papers.

4. **The pipeline STOPPED after Stage 4 started.** The log shows `[4/5] Renderer — Document → LaTeX [ieee]` but no output path, no Stage 5, no `RUN END`. This means the renderer crashed.

---

## 6. BUGS FOUND (Root Cause Analysis)

### BUG #1: 🔴 CRITICAL — Pipeline crashes at Stage 4 (Renderer)

**Symptom**: Log ends at `[4/5] Renderer` — no `.tex` output, no Stage 5.

**Root cause**: The `template/renderer.py` calls `env.get_template(TEMPLATE_FILE)` where `TEMPLATE_FILE = "template.tex.j2"`. BUT look at the IEEE Jinja2 template — it uses a Jinja2 `select` filter:

```jinja
\VAR{([author.city, author.country] | select | list | join(", ") | e)}
```

The `select` filter with no arguments returns truthy items. This is valid Jinja2, but if `author.city` or `author.country` is `""` (empty string — which it will be from the AI parser defaults), the `select` filter drops it, `list` converts, `join` joins. This should work fine actually.

**More likely cause**: Look at the `_make_env()` in `template/renderer.py` — it does NOT register a `select` filter nor does it use `autoescape=False` with `map("e")` correctly. The `map("e")` filter in the templates calls the `e` filter on each element. For example in `ieee/template.tex.j2`:

```jinja
\VAR{keywords | map("e") | join(", ")}
```

If `keywords` is a list of strings, `map("e")` applies `latex_escape` to each — this should work. BUT if the Jinja2 Environment doesn't have the built-in `select` filter available... Actually, `select` IS a built-in Jinja2 filter, so that's fine.

**REAL root cause — check the exception handling**: In `pipeline.py`, `_render()` has:
```python
except Exception as e:
    log_error("Renderer", e, fatal=True)
    raise
```

If `log_error` fails (e.g. the log was already closed or has an issue), the error message is lost. But more critically: **the log shows the stage START but no error message**, which means either:
- The error was raised but not caught properly
- Or the process was killed (OOM, timeout, etc.)

**Most probable root cause**: The `generated.tex` intermediate file that exists in project knowledge shows RAW UNPROCESSED TEXT being dumped — it contains literal Unicode characters like `$\in$`, `$\cup$`, broken ligatures, and page header/footer junk (`1 3 Page 5 of 36   72 Machine Learning (2026) 115:72`). This means the AI parser is returning body text with noise that wasn't cleaned, AND the rendered `.tex` may contain illegal LaTeX characters that crash pdflatex.

But the crash is at the RENDERER stage (Jinja2), not the compiler. So the Jinja2 template itself may be erroring on unexpected data.

**Action items**:
1. Wrap the renderer in better try/except with full traceback logging
2. Validate the Document object before rendering (non-empty sections, valid strings)
3. Add a `--verbose` flag to surface Stage 4 errors to console

---

### BUG #2: 🟡 HIGH — AI parser returns only 1 section for entire paper

**Symptom**: `AI parse complete — 1 sections, 0 tables, 5 refs`

**Root cause**: The body batch prompt in `ai/structure_llm.py` asks for sections, but Qwen3-8b at this speed/quality level is likely returning the entire body as one section blob instead of splitting it into Introduction, Related Work, etc.

The prompt says:
```
"heading": "Section Name (clean, no numbering like '1.' or 'I.')"
```

But the model may be outputting a single section with heading "Introduction" and the entire body crammed into one blob.

**Impact**: The IEEE template generates one `\section{...}` with ALL text in it — the paper will look wrong but won't necessarily crash.

**Fix**:
1. Post-process: if only 1 section is returned and body > 2000 chars, attempt to split it by heading patterns
2. Fall back to heuristic parser when AI returns < 2 sections for a paper > 3000 chars
3. Improve the prompt to explicitly demand multiple sections

---

### BUG #3: 🟡 HIGH — `stages/` folder may still exist (import shadowing)

**Symptom**: `check/structure.py` explicitly warns about this.

**Root cause**: Old refactoring left dead code in `stages/`. If this folder exists, Python may import from `stages/ai_structure_detector.py` instead of `ai/structure_llm.py` because of `sys.path` configuration.

**Evidence**: The old `stages/ai_structure_detector.py` imports from `stages.document_parser` which has a different API:
```python
from stages.document_parser import _extract_references_from_text  # old
# vs
from ai.heuristic_parser import extract_references                # new
```

**Fix**: Delete the entire `stages/` folder. It is dead code.

---

### BUG #4: 🟡 MEDIUM — `stages/template_renderer.py` has wrong function signature

**Symptom**: If the old `stages/template_renderer.py` is accidentally imported, it has:
```python
def render(doc, template_dir, template_name, output_tex)  # old — template_dir first
```

But the new `template/renderer.py` has:
```python
def render(doc, template_name, template_dir, output_tex)  # new — template_name first
```

**AND** the pipeline calls it as:
```python
from template.renderer import render as do_render
return do_render(doc, template_name, tdir, output_tex)
```

This is correct for the NEW renderer. But if the old `stages/` code is somehow imported, the arguments are swapped → crash.

**Fix**: Delete `stages/` folder (same as Bug #3).

---

### BUG #5: 🟡 MEDIUM — Duplicate `latex_escape` implementations

There are THREE copies of `latex_escape` / `latex_escape_paragraphs`:
1. `mapper/base_mapper.py` (the canonical one, imported by `template/renderer.py`)
2. `stages/template_renderer.py` (dead code, old)
3. The functions are also used inline in templates

The new renderer correctly imports from `mapper/base_mapper.py`. No functional bug unless `stages/` interferes.

---

### BUG #6: 🟢 LOW — Slow LLM performance (not a code bug)

**Header extraction**: 228 seconds for 3000 chars → ~2 tokens/sec
**Body batch**: 471 seconds for 2949 chars → ~4.6 tokens/sec

This is extremely slow for Qwen3-8b. Likely causes:
- GPU not being used (CPU-only inference)
- LM Studio quantization settings
- Model is spending time in `<think>` tags (Qwen3 has chain-of-thought)

The `_parse_json()` function in `ai/structure_llm.py` correctly strips `<think>` tags:
```python
text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)  # unclosed tag
```

**Fix**: Not a code fix — hardware/model configuration issue.

---

### BUG #7: 🟢 LOW — `cleaning_llm.py` Reference cleaning is incomplete

In `ai/cleaning_llm.py`, there's a comment:
```python
# FIX: Reference objects must be cleaned field-by-field, not as strings
doc.references = [Reference(index...
```

The code is cut off in the knowledge base, but based on the pattern, it should be:
```python
doc.references = [Reference(index=r.index, text=_clean(r.text)) for r in doc.references]
```

If this line is incomplete or wrong, references won't be cleaned of ligatures/unicode.

---

## 7. GENERATED TEX QUALITY CHECK

The `intermediate/generated.tex` in the knowledge base shows:

```latex
1 3 Page 5 of 36   72 Machine Learning (2026) 115:72
```

This is **page header junk** from PDF extraction that leaked through all stages. The normalizer (Stage 3) doesn't strip these. The AI parser should have excluded them from section bodies.

Other issues in generated.tex:
- `$\in$` and `$\cup$` appear as literal text (should be LaTeX math, which they are — but double-escaping may occur)
- `\section{Related Work}` appears inline in body text (heading leaked into body)
- Line-break artifacts: `col­ laborate` (hyphenation from PDF column layout)

---

## 8. TEMPLATE SYSTEM

All templates use the same Jinja2 syntax with LaTeX-safe delimiters:

```
Block:    \BLOCK{...}      (instead of {% %})
Variable: \VAR{...}        (instead of {{ }})
Comment:  \#{...}          (instead of {# #})
Line stmt: %%              (prefix)
```

Filters registered:
- `e` → `latex_escape` (escape LaTeX specials, preserve math)
- `paras` → `latex_escape_paragraphs` (escape + preserve %%RAWTEX%% blocks)

The IEEE template expects these Document attributes via `doc.to_dict_with_objects()`:
- `title`, `authors` (list of Author objects), `abstract`, `keywords` (list of str)
- `sections` (list of Section objects with `.heading`, `.body`, `.tables`)
- `references` (list of Reference objects with `.index`, `.text`)

---

## 9. RECOMMENDED FIXES (Priority Order)

### P0 — Get the pipeline completing end-to-end

1. **Delete `stages/` folder** entirely — it's dead code that shadows imports
2. **Add better error handling in Stage 4** — catch and log the actual Jinja2 error:
   ```python
   try:
       tex = env.get_template(TEMPLATE_FILE).render(**doc.to_dict_with_objects())
   except Exception as e:
       log.error(f"Jinja2 render failed: {e}", exc_info=True)
       log.error(f"Doc stats: sections={len(doc.sections)}, refs={len(doc.references)}")
       raise
   ```
3. **Validate Document before rendering** — check for None/empty fields that would crash Jinja2

### P1 — Fix AI quality issues

4. **Add fallback logic**: if AI returns only 1 section for a multi-page paper, fall back to heuristic parser
5. **Strip PDF header/footer junk** from extracted text before AI parsing:
   ```python
   # Remove repeated page headers like "Machine Learning (2026) 115:72"
   text = re.sub(r"^\d+\s+\d+\s+Page \d+ of \d+.*$", "", text, flags=re.MULTILINE)
   ```
6. **Fix hyphenation artifacts**: `col­ laborate` → `collaborate`
   ```python
   text = re.sub(r"(\w)­\s+(\w)", r"\1\2", text)  # soft hyphen + whitespace
   ```

### P2 — Robustness

7. **Complete the `cleaning_llm.py` Reference cleaning** (Bug #7)
8. **Add structured.json validation** against `schema/paper_schema.json` between Stage 2 and Stage 3
9. **Add a `--dry-run` flag** that stops after Stage 4 (produces .tex but no PDF)

---

## 10. HOW TO RUN / TEST

```bash
# Pre-flight check
python check/structure.py

# Normal run
python main.py input/your_paper.pdf --template ieee

# Without AI (faster, heuristic only)
python main.py input/your_paper.pdf --template ieee --no-ai

# Check intermediate outputs
cat intermediate/extracted.txt          # Raw text from PDF
cat intermediate/structured.json        # Parsed document structure
cat intermediate/generated.tex          # LaTeX output
cat logs/pipeline_latest.log            # Full debug log
```

---

## 11. QUICK REFERENCE: Import Graph

```
main.py
  └→ core.pipeline.run()
       ├→ extractor.pdf_extractor.extract()    [Stage 1]
       ├→ ai.structure_llm.parse()             [Stage 2A - AI]
       │   └→ ai.heuristic_parser.extract_references()  [fallback refs]
       ├→ ai.heuristic_parser.parse()           [Stage 2B - heuristic fallback]
       │   └→ mapper.base_mapper.inject_tables()
       ├→ ai.cleaning_llm.normalize()           [Stage 3]
       ├→ template.renderer.render()            [Stage 4]
       │   └→ mapper.base_mapper.latex_escape()
       │   └→ mapper.base_mapper.latex_escape_paragraphs()
       └→ compiler.latex_compiler.compile()     [Stage 5]
```

---

## 12. ENVIRONMENT REQUIREMENTS

```
Python 3.10+
pdfplumber, pypdf, pymupdf (fitz), python-docx
requests (CRITICAL — fixes urllib streaming timeout)
jinja2
pdflatex (texlive-full on Linux, MacTeX on macOS, MiKTeX on Windows)
LM Studio running locally on port 1234 with qwen3-8b loaded
```