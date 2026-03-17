"""
core/pipeline.py
Stage order:
  1. layout_parser        PDF/DOCX  → extracted.txt
  2. ai_structure_detector OR document_parser → Document
  3. normalizer           Document  → Document (cleaned)
  4. template_renderer    Document  → generated.tex
  5. latex_compiler       .tex      → output PDF
"""

import os
from core import config
from stages import layout_parser, document_parser, normalizer, template_renderer, latex_compiler


def _ensure_dirs():
    for d in (config.INPUT_DIR, config.INTERMEDIATE_DIR, config.OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)


def run(input_file: str, output_dir: str = None) -> str:
    out_dir = output_dir or config.OUTPUT_DIR
    _ensure_dirs()
    os.makedirs(out_dir, exist_ok=True)

    _log_header(input_file)

    _log_stage(1, "Layout Parser", "PDF/DOCX → raw text blocks")
    layout_parser.parse(input_file, config.EXTRACTED_TXT)

    _log_stage(2, "Document Parser", "raw text → structured Document")
    doc = _parse_document(config.EXTRACTED_TXT)
    doc.to_json(config.STRUCTURED_JSON)

    _log_stage(3, "Content Normalizer", "fix ligatures, encoding, hyphenation")
    doc = normalizer.normalize(doc)
    doc.to_json(config.STRUCTURED_JSON)

    _log_stage(4, "Template Renderer", "Document → LaTeX")
    template_renderer.render(doc, config.TEMPLATE_DIR, config.TEMPLATE_NAME, config.GENERATED_TEX)

    _log_stage(5, "LaTeX Compiler", ".tex → IEEE PDF")
    pdf_path = latex_compiler.compile(config.GENERATED_TEX, out_dir)

    print(f"\n  ✅  {pdf_path}\n")
    return pdf_path


def _parse_document(extracted_path: str):
    """Try AI detector first; fall back to heuristic parser."""
    try:
        from stages.ai_structure_detector import parse as ai_parse
        doc = ai_parse(extracted_path)
        if doc is not None:
            return doc
    except Exception as e:
        print(f"         [AI] Error: {e} — falling back to heuristic parser")

    print("         [heuristic] parsing document structure...")
    return document_parser.parse(extracted_path)


def _log_header(input_file: str):
    print(f"\n{'─' * 52}")
    print(f"  IEEE Formatter")
    print(f"  {input_file}")
    print(f"{'─' * 52}")

def _log_stage(n: int, name: str, desc: str):
    print(f"\n  [{n}/5] {name}")
    print(f"         {desc}")