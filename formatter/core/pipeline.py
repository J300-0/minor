"""
core/pipeline.py
The orchestrator. Calls each stage in order, wires up paths from config.
app.py and run_pipeline.py both call this — no logic is duplicated.

Stage order:
  1. layout_parser      PDF/DOCX  → extracted.txt
  2. document_parser    .txt      → Document (structured.json)
  3. normalizer         Document  → Document (cleaned in-place)
  4. template_renderer  Document  → generated.tex
  5. latex_compiler     .tex      → output PDF
"""

import os
from core import config
from stages import layout_parser, document_parser, normalizer, template_renderer, latex_compiler


def _ensure_dirs():
    for d in (config.INPUT_DIR, config.INTERMEDIATE_DIR, config.OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)


def run(input_file: str, output_dir: str = None) -> str:
    """
    Run the full pipeline on input_file.
    Returns the path to the final PDF.
    """
    out_dir = output_dir or config.OUTPUT_DIR
    _ensure_dirs()
    os.makedirs(out_dir, exist_ok=True)

    _log_header(input_file)

    # ── Stage 1: Layout Parser ────────────────────────────────────────────────
    _log_stage(1, "Layout Parser", "PDF/DOCX → raw text blocks")
    layout_parser.parse(input_file, config.EXTRACTED_TXT)

    # ── Stage 2: Document Parser ──────────────────────────────────────────────
    _log_stage(2, "Document Parser", "raw text → structured Document")
    doc = document_parser.parse(config.EXTRACTED_TXT)
    doc.to_json(config.STRUCTURED_JSON)

    # ── Stage 3: Content Normalizer ───────────────────────────────────────────
    _log_stage(3, "Content Normalizer", "fix ligatures, encoding, hyphenation")
    doc = normalizer.normalize(doc)
    doc.to_json(config.STRUCTURED_JSON)   # overwrite with cleaned version

    # ── Stage 4: Template Renderer ────────────────────────────────────────────
    _log_stage(4, "Template Renderer", "Document → LaTeX")
    template_renderer.render(doc, config.TEMPLATE_DIR, config.TEMPLATE_NAME, config.GENERATED_TEX)

    # ── Stage 5: LaTeX Compiler ───────────────────────────────────────────────
    _log_stage(5, "LaTeX Compiler", ".tex → IEEE PDF")
    pdf_path = latex_compiler.compile(config.GENERATED_TEX, out_dir)

    print(f"\n    {pdf_path}\n")
    return pdf_path


# ── Internal logging helpers ──────────────────────────────────────────────────

def _log_header(input_file: str):
    print(f"\n{'─' * 52}")
    print(f"  IEEE Formatter")
    print(f"  {input_file}")
    print(f"{'─' * 52}")

def _log_stage(n: int, name: str, desc: str):
    print(f"\n  [{n}/5] {name}")
    print(f"         {desc}")