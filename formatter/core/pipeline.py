"""
core/pipeline.py  —  Orchestrates all 5 stages

Stage flow:
  1. Extract   PDF/DOCX → {raw_text, tables, images}
  2. Parse     raw text → Document
  3. Normalize Document → Document (cleaned)
  4. Render    Document → .tex
  5. Compile   .tex     → PDF
"""

import os, time
from core import config
from core.models import Document
from core.logger import (get_logger, log_run_start, log_stage, log_doc_stats,
                          log_extraction, log_refs, log_error, log_run_end)

log = get_logger(__name__)


def run(input_file: str, template: str = None, output_dir: str = None,
        use_ai: bool = True) -> str:

    template  = (template or config.DEFAULT_TEMPLATE).lower()
    out_dir   = output_dir or config.OUTPUT_DIR
    inter_dir = config.INTERMEDIATE_DIR
    t0        = time.time()

    _validate(input_file, template)
    _ensure_dirs(out_dir, inter_dir)
    log_run_start(input_file, template, use_ai)
    _print_header(input_file, template)

    # ── Stage 1: Extract ──────────────────────────────────────────────────────
    log_stage(1, "Extractor", "PDF/DOCX → text + tables + images")
    _log_stage_print(1, "Extractor", "PDF/DOCX → text + tables + images")
    rich = _extract(input_file, inter_dir)
    log_extraction(len(rich["raw_text"]), len(rich["tables"]), len(rich["images"]))

    # ── Stage 2: Parse ────────────────────────────────────────────────────────
    log_stage(2, "Parser", "raw text → structured Document")
    _log_stage_print(2, "Parser", "raw text → structured Document")
    doc = _parse(config.EXTRACTED_TXT, rich, use_ai)
    doc.to_json(config.STRUCTURED_JSON)
    log_doc_stats(doc)
    log_refs(len(doc.references), "heuristic/AI")
    _print_doc_stats(doc)

    # ── Stage 3: Normalize ────────────────────────────────────────────────────
    log_stage(3, "Normalizer", "fix ligatures, unicode, math symbols")
    _log_stage_print(3, "Normalizer", "fix ligatures, unicode, math symbols")
    from ai.cleaning_llm import normalize
    doc = normalize(doc)
    doc.to_json(config.STRUCTURED_JSON)

    # ── Stage 4: Render ───────────────────────────────────────────────────────
    log_stage(4, "Renderer", f"Document → LaTeX [{template}]")
    _log_stage_print(4, "Renderer", f"Document → LaTeX [{template}]")
    tex_path = _render(doc, template, config.GENERATED_TEX)

    # ── Stage 5: Compile ──────────────────────────────────────────────────────
    log_stage(5, "Compiler", ".tex → PDF")
    _log_stage_print(5, "Compiler", ".tex → PDF")
    from compiler.latex_compiler import compile as latex_compile
    pdf_path = latex_compile(tex_path, out_dir)

    elapsed = time.time() - t0
    log_run_end(pdf_path, elapsed)
    print(f"\n  ✅  Done → {pdf_path}  ({elapsed:.1f}s)\n")
    return pdf_path


# ── Stage implementations ─────────────────────────────────────────────────────

def _extract(input_file: str, inter_dir: str) -> dict:
    ext = os.path.splitext(input_file)[1].lower()
    try:
        if ext == ".pdf":
            from extractor.pdf_extractor import extract
            return extract(input_file, inter_dir)
        else:
            from extractor.docx_extractor import extract
            return extract(input_file, inter_dir)
    except Exception as e:
        log_error("Extractor", e, fatal=False)
        log.warning("Continuing with empty extraction — output may be blank")
        return {"raw_text": "", "tables": [], "images": []}


def _parse(extracted_path: str, rich: dict, use_ai: bool) -> Document:
    assert isinstance(rich, dict), f"rich must be dict, got {type(rich)}"

    if use_ai:
        try:
            from ai.structure_llm import parse as ai_parse
            doc = ai_parse(extracted_path, rich)
            if doc is not None:
                log.info("       parse: AI path succeeded")
                return doc
        except Exception as e:
            log_error("AI Parser", e, fatal=False)
            log.warning("Falling back to heuristic parser")

    log.info("       parse: using heuristic parser")
    print("         [heuristic] parsing document structure...")
    from ai.heuristic_parser import parse as heuristic_parse
    return heuristic_parse(extracted_path, rich)


def _render(doc: Document, template_name: str, output_tex: str) -> str:
    tdir = os.path.join(config.TEMPLATES_DIR, template_name)
    if not os.path.isdir(tdir):
        raise FileNotFoundError(
            f"Template folder not found: {tdir}\n"
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )
    tmpl_file = os.path.join(tdir, "template.tex.j2")
    if not os.path.exists(tmpl_file):
        raise FileNotFoundError(f"template.tex.j2 not found in {tdir}")
    try:
        from template.renderer import render as do_render
        return do_render(doc, template_name, tdir, output_tex)
    except Exception as e:
        log_error("Renderer", e, fatal=True)
        raise



# ── Validation & helpers ──────────────────────────────────────────────────────

def _validate(input_file: str, template: str):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
    ext = os.path.splitext(input_file)[1].lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported: {ext}. Supported: {config.SUPPORTED_EXTENSIONS}")
    if template not in config.TEMPLATE_REGISTRY:
        raise ValueError(
            f"Unknown template '{template}'. "
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )


def _ensure_dirs(*dirs):
    for d in (config.INPUT_DIR, config.INTERMEDIATE_DIR, *dirs):
        os.makedirs(d, exist_ok=True)


def _print_header(input_file, template):
    print(f"\n{'─' * 56}")
    print(f"  AI Paper Formatter")
    print(f"  input:    {os.path.basename(input_file)}")
    print(f"  template: {template}")
    print(f"{'─' * 56}")


def _log_stage_print(n, name, desc):
    print(f"\n  [{n}/5] {name}")
    print(f"         {desc}")


def _print_doc_stats(doc: Document):
    n_tables = sum(len(s.tables) for s in doc.sections)
    print(f"         sections={len(doc.sections)}  "
          f"tables={n_tables}  refs={len(doc.references)}")