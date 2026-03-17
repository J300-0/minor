"""
core/pipeline.py  —  Orchestrates all 5 stages

Stage flow:
  1. Extract   PDF/DOCX → {raw_text, tables, images}   (extractor/)
  2. Parse     raw text → Document                      (ai/ or heuristic)
  3. Normalize Document → Document (cleaned)            (ai/cleaning_llm)
  4. Render    Document → .tex                          (templates/<name>/mapper)
  5. Compile   .tex     → PDF                           (compiler/)

Key design decisions:
  - layout_parser always returns a dict (never None/str) — crash bug fixed
  - rich dict is threaded into BOTH parse paths so tables are never lost
  - template is selected by name via TEMPLATE_REGISTRY in config
  - --no-ai flag disables LM Studio and forces heuristic parser
"""

import os, sys
from core import config
from core.models import Document


def run(input_file: str, template: str = None, output_dir: str = None, use_ai: bool = True) -> str:
    """
    Run the full pipeline.

    Args:
        input_file:  path to PDF or DOCX
        template:    one of ieee/acm/springer/elsevier/apa/arxiv  (default: ieee)
        output_dir:  where to write the final PDF  (default: output/)
        use_ai:      try LM Studio first; fall back to heuristic  (default: True)
    """
    template   = (template or config.DEFAULT_TEMPLATE).lower()
    out_dir    = output_dir or config.OUTPUT_DIR
    inter_dir  = config.INTERMEDIATE_DIR

    _validate(input_file, template)
    _ensure_dirs(out_dir, inter_dir)
    _log_header(input_file, template)

    # ── Stage 1: Extract ──────────────────────────────────────────────────────
    _log_stage(1, "Extractor", "PDF/DOCX → text + tables + images")
    rich = _extract(input_file, inter_dir)
    # rich is ALWAYS a dict {raw_text, tables, images} — never None

    # ── Stage 2: Parse ────────────────────────────────────────────────────────
    _log_stage(2, "Parser", "raw text → structured Document")
    doc = _parse(config.EXTRACTED_TXT, rich, use_ai)
    doc.to_json(config.STRUCTURED_JSON)
    _log_doc_stats(doc)

    # ── Stage 3: Normalize ────────────────────────────────────────────────────
    _log_stage(3, "Normalizer", "fix ligatures, unicode, math symbols")
    from ai.cleaning_llm import normalize
    doc = normalize(doc)
    doc.to_json(config.STRUCTURED_JSON)

    # ── Stage 4: Render ───────────────────────────────────────────────────────
    _log_stage(4, "Renderer", f"Document → LaTeX  [{template}]")
    tex_path = _render(doc, template, config.GENERATED_TEX)

    # ── Stage 5: Compile ──────────────────────────────────────────────────────
    _log_stage(5, "Compiler", ".tex → PDF")
    from compiler.latex_compiler import compile as latex_compile
    pdf_path = latex_compile(tex_path, out_dir)

    print(f"\n  ✅  Done → {pdf_path}\n")
    return pdf_path


# ── Stage implementations ─────────────────────────────────────────────────────

def _extract(input_file: str, inter_dir: str) -> dict:
    """Route to pdf_extractor or docx_extractor. Always returns a dict."""
    ext = os.path.splitext(input_file)[1].lower()
    try:
        if ext == ".pdf":
            from extractor.pdf_extractor import extract
            return extract(input_file, inter_dir)
        else:
            from extractor.docx_extractor import extract
            return extract(input_file, inter_dir)
    except Exception as e:
        print(f"         [extract] error: {e} — continuing with empty extraction")
        return {"raw_text": "", "tables": [], "images": []}


def _parse(extracted_path: str, rich: dict, use_ai: bool) -> Document:
    """
    Try AI parser first (if use_ai=True and LM Studio running).
    Falls back to heuristic parser.
    rich is always a dict — never a str or None.
    """
    assert isinstance(rich, dict), f"rich must be dict, got {type(rich)}"

    if use_ai:
        try:
            from ai.structure_llm import parse as ai_parse
            doc = ai_parse(extracted_path, rich)
            if doc is not None:
                return doc
        except Exception as e:
            print(f"         [AI] error: {e} — falling back to heuristic")

    print("         [heuristic] parsing document structure...")
    from ai.heuristic_parser import parse as heuristic_parse
    return heuristic_parse(extracted_path, rich)


def _render(doc: Document, template_name: str, output_tex: str) -> str:
    """Load the template-specific mapper and render to .tex."""
    tdir   = os.path.join(config.TEMPLATES_DIR, template_name)
    mapper = _load_mapper(template_name)
    return mapper.render(doc, tdir, output_tex)


def _load_mapper(template_name: str):
    """
    Dynamically import templates/<name>/mapper.py.
    This is how new templates are added — no changes to pipeline needed.
    """
    import importlib.util, sys as _sys
    mapper_path = os.path.join(config.TEMPLATES_DIR, template_name, "mapper.py")
    if not os.path.exists(mapper_path):
        raise FileNotFoundError(
            f"No mapper found for template '{template_name}'.\n"
            f"Expected: {mapper_path}\n"
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )
    spec   = importlib.util.spec_from_file_location(f"templates.{template_name}.mapper", mapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Validation & helpers ──────────────────────────────────────────────────────

def _validate(input_file: str, template: str):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
    ext = os.path.splitext(input_file)[1].lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Supported: {config.SUPPORTED_EXTENSIONS}")
    if template not in config.TEMPLATE_REGISTRY:
        raise ValueError(
            f"Unknown template '{template}'. "
            f"Available: {list(config.TEMPLATE_REGISTRY.keys())}"
        )


def _ensure_dirs(*dirs):
    for d in (config.INPUT_DIR, config.INTERMEDIATE_DIR, *dirs):
        os.makedirs(d, exist_ok=True)


def _log_header(input_file, template):
    print(f"\n{'─' * 56}")
    print(f"  AI Paper Formatter")
    print(f"  input:    {os.path.basename(input_file)}")
    print(f"  template: {template}")
    print(f"{'─' * 56}")


def _log_stage(n, name, desc):
    print(f"\n  [{n}/5] {name}")
    print(f"         {desc}")


def _log_doc_stats(doc: Document):
    n_tables = sum(len(s.tables) for s in doc.sections)
    print(f"         sections={len(doc.sections)}  tables={n_tables}  refs={len(doc.references)}")