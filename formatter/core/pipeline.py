"""
core/pipeline.py — Orchestrates the 5-stage (+canon) formatting pipeline.

Stage flow:
  1.   Extract    PDF/DOCX -> raw text + tables + images
  2.   Parse      raw text -> Document  (heuristic, font-aware)
  3.   Normalize  Document -> Document  (ligatures, unicode, math)
  2.5  Canon      Document -> CanonicalDocument  (validate + repair + score)
  4.   Render     Document -> .tex      (Jinja2)
  5.   Compile    .tex     -> PDF       (pdflatex)
"""
import os
import time

from core import config
from core.models import Document
from core.logger import (
    get_logger, log_run_start, log_stage,
    log_extraction, log_doc_stats, log_error, log_run_end,
)

log = get_logger(__name__)


def run(input_file: str, template: str = None, output_dir: str = None) -> str:
    """
    Run the full formatting pipeline.
    Returns path to the output PDF.
    """
    template = (template or config.DEFAULT_TEMPLATE).lower()
    out_dir = output_dir or config.OUTPUT_DIR
    t0 = time.time()

    _validate(input_file, template)
    _ensure_dirs(out_dir)
    log_run_start(input_file, template)
    _banner(input_file, template)

    # ── 1. Extract ───────────────────────────────────────────────────────────
    log_stage(1, "Extractor", "PDF/DOCX -> text + tables + images")
    _print_stage(1, "Extract", "PDF/DOCX -> raw text")
    rich = _extract(input_file)
    log_extraction(len(rich["raw_text"]), len(rich["tables"]), len(rich["images"]))

    # ── 2. Parse ─────────────────────────────────────────────────────────────
    log_stage(2, "Parser", "raw text -> structured Document")
    _print_stage(2, "Parse", "raw text -> Document structure")
    doc = _parse(rich)
    doc.to_json(config.STRUCTURED_JSON)
    log_doc_stats(doc)
    print(f"         sections={len(doc.sections)}  refs={len(doc.references)}")

    # ── 3. Normalize ─────────────────────────────────────────────────────────
    log_stage(3, "Normalizer", "fix ligatures, unicode, math")
    _print_stage(3, "Normalize", "fix ligatures, unicode, math")
    from normalizer.cleaner import normalize
    doc = normalize(doc)
    doc.to_json(config.STRUCTURED_JSON)

    # ── 2.5. Canonical Builder (gate) ────────────────────────────────────────
    log_stage("2.5", "Canon", "validate + repair + score Document")
    _print_stage("2.5", "Canon", "validate + repair + score")
    from canon.builder import build_canonical
    canon_doc = build_canonical(doc)

    print(f"         confidence={canon_doc.overall_confidence:.2f}  "
          f"renderable={canon_doc.is_renderable()}  "
          f"repairs={len(canon_doc.repair_log)}")

    if canon_doc.warnings:
        for w in canon_doc.warnings:
            print(f"         !  {w}")
            log.warning("[CANON] %s", w)

    if not canon_doc.is_renderable():
        log.error("Document failed canonical check. Summary:\n%s",
                  canon_doc.summary())
        raise RuntimeError(
            f"Document is not renderable after canonical check. "
            f"Confidence: {canon_doc.overall_confidence:.2f}. "
            f"Check logs for details."
        )

    # Unwrap back to plain Document for renderer
    doc = canon_doc.to_document()
    doc.to_json(config.STRUCTURED_JSON)

    # ── 4. Render ────────────────────────────────────────────────────────────
    log_stage(4, "Renderer", f"Document -> LaTeX [{template}]")
    _print_stage(4, "Render", f"Document -> LaTeX [{template}]")
    tex_path = _render(doc, template)

    # ── 5. Compile ───────────────────────────────────────────────────────────
    log_stage(5, "Compiler", "LaTeX -> PDF")
    _print_stage(5, "Compile", "LaTeX -> PDF")
    pdf_path = _compile(tex_path, template, out_dir)

    elapsed = round(time.time() - t0, 1)
    log_run_end(pdf_path, elapsed)
    _print_done(pdf_path, elapsed)
    return pdf_path


# ══════════════════════════════════════════════════════════════════════════════
# Stage implementations
# ══════════════════════════════════════════════════════════════════════════════

def _extract(input_file: str) -> dict:
    ext = os.path.splitext(input_file)[1].lower()
    try:
        if ext == ".pdf":
            from extractor.pdf_extractor import extract
        else:
            from extractor.docx_extractor import extract
        return extract(input_file, config.INTERMEDIATE_DIR)
    except Exception as e:
        log_error("Extractor", e, fatal=False)
        log.warning("Extraction failed — continuing with empty content")
        return {"raw_text": "", "blocks": [], "tables": [], "images": []}


def _parse(rich: dict) -> Document:
    raw = rich.get("raw_text", "")
    os.makedirs(config.INTERMEDIATE_DIR, exist_ok=True)
    with open(config.EXTRACTED_TXT, "w", encoding="utf-8") as f:
        f.write(raw)
    from parser.heuristic import parse
    return parse(rich)


def _render(doc: Document, template: str) -> str:
    tdir = os.path.join(config.TEMPLATES_DIR, template)
    if not os.path.isdir(tdir):
        raise FileNotFoundError(
            f"Template folder not found: {tdir}\n"
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )
    from renderer.jinja_renderer import render
    return render(doc, template, tdir, config.GENERATED_TEX)


def _compile(tex_path: str, template: str, out_dir: str) -> str:
    from compiler.latex_compiler import compile as latex_compile
    return latex_compile(tex_path, out_dir, template)


# ══════════════════════════════════════════════════════════════════════════════
# Validation and directory helpers
# ══════════════════════════════════════════════════════════════════════════════

def _validate(input_file: str, template: str):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
    if template not in config.TEMPLATE_REGISTRY:
        raise ValueError(
            f"Unknown template {template!r}. "
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )


def _ensure_dirs(out_dir: str):
    for d in (config.INTERMEDIATE_DIR, out_dir, config.LOGS_DIR):
        os.makedirs(d, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Console output helpers
# ══════════════════════════════════════════════════════════════════════════════

def _banner(input_file: str, template: str):
    print("\n" + "=" * 60)
    print("  ai-paper-formatter")
    print(f"  input    : {os.path.basename(input_file)}")
    print(f"  template : {template.upper()}")
    print("=" * 60)


def _print_stage(n, name: str, desc: str):
    print(f"\n[{n}] {name:<12} {desc}")
    print(f"     {'-' * 45}")


def _print_done(pdf_path: str, elapsed: float):
    print("\n" + "=" * 60)
    print(f"  Done in {elapsed}s")
    print(f"  Output: {pdf_path}")
    print("=" * 60 + "\n")
