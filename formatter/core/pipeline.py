"""
core/pipeline.py — 6-stage orchestrator.

Extract → Parse → Canon → Normalize → Render → Compile
"""
import json
import os
import time

from core.config import (
    INTERMEDIATE_DIR,
    OUTPUT_DIR,
    TEMPLATE_REGISTRY,
)
from core.logger import get_logger
from core.models import Document

log = get_logger()


def run(input_file: str, template: str = "ieee", output_dir: str = None) -> str:
    """
    Run the full pipeline.  Returns path to generated PDF.
    """
    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    if template not in TEMPLATE_REGISTRY:
        raise ValueError(f"Unknown template '{template}'. Choose from: {list(TEMPLATE_REGISTRY.keys())}")

    out_dir = output_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    ext = os.path.splitext(input_file)[1].lower()
    t0 = time.time()

    # ── Stage 1: Extract ──────────────────────────────────────
    log.info("Stage 1/6: Extract (%s)", ext)
    _flush_log(log)
    if ext == ".pdf":
        from extractor.pdf_extractor import extract_pdf
        raw = extract_pdf(input_file)
    elif ext in (".docx", ".doc"):
        from extractor.docx_extractor import extract_docx
        raw = extract_docx(input_file)
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    _flush_log(log)

    # Save intermediate extracted text
    with open(os.path.join(INTERMEDIATE_DIR, "extracted.txt"), "w", encoding="utf-8") as f:
        f.write(raw.get("text", ""))

    # ── Stage 2: Parse ────────────────────────────────────────
    log.info("Stage 2/6: Parse")
    _flush_log(log)
    from parser.heuristic import parse_document
    doc = parse_document(raw)

    _flush_log(log)

    # Attach formula blocks (filter low-confidence results)
    formula_blocks = raw.get("formula_blocks", [])
    if formula_blocks:
        from core.models import FormulaBlock
        good_fbs = [
            FormulaBlock(**fb) if isinstance(fb, dict) else fb
            for fb in formula_blocks
            if (fb.get("confidence", 0) if isinstance(fb, dict) else fb.confidence) >= 0.45
        ]
        kept = len(good_fbs)
        total = len(formula_blocks)
        log.info("  Formula blocks: %d kept / %d total (conf >= 0.45)", kept, total)

        # Distribute formula blocks into sections by page proximity
        _attach_formulas_to_sections(doc, good_fbs)

        # Any formulas that couldn't be placed go into the document-level list
        # (rendered as "Key Equations" at the end by templates)
        placed = set()
        for s in doc.sections:
            for fb in s.formula_blocks:
                placed.add(id(fb))
        doc.formula_blocks = [fb for fb in good_fbs if id(fb) not in placed]
        if doc.formula_blocks:
            log.info("  %d formula blocks placed in sections, %d in Key Equations",
                     kept - len(doc.formula_blocks), len(doc.formula_blocks))

    # Attach extracted figures to sections (distribute by page proximity)
    raw_figures = raw.get("figures", [])
    if raw_figures:
        from core.models import Figure
        figures = [
            Figure(**fig) if isinstance(fig, dict) else fig
            for fig in raw_figures
        ]
        _attach_figures_to_sections(doc, figures)
        log.info("  Figures: %d extracted", len(figures))

    # Save structured JSON
    _save_structured_json(doc)
    _flush_log(log)

    # ── Stage 3: Canon (validate + repair) ────────────────────
    log.info("Stage 3/6: Canon")
    from canon.builder import build_canonical
    canon_doc = build_canonical(doc)

    if not canon_doc.is_renderable():
        summary = canon_doc.summary()
        log.error("Document not renderable:\n%s", summary)
        raise RuntimeError(f"Document not renderable. Check logs/pipeline_latest.log\n{summary}")

    log.info("  Canon OK: %s", canon_doc.summary())
    doc = canon_doc.to_document()
    _flush_log(log)

    # ── Stage 4: Normalize ────────────────────────────────────
    log.info("Stage 4/6: Normalize")
    from normalizer.cleaner import normalize
    doc = normalize(doc)
    _flush_log(log)

    # ── Stage 5: Render ───────────────────────────────────────
    log.info("Stage 5/6: Render (%s)", template)
    from renderer.jinja_renderer import render
    tex_path = render(doc, template)
    log.info("  Generated: %s", tex_path)

    # ── Stage 6: Compile ──────────────────────────────────────
    log.info("Stage 6/6: Compile")
    from compiler.latex_compiler import compile_latex
    pdf_path = compile_latex(tex_path, out_dir, template)

    elapsed = time.time() - t0
    log.info("Done in %.1fs → %s", elapsed, pdf_path)
    return pdf_path


def _attach_formulas_to_sections(doc: Document, formula_blocks: list):
    """
    Distribute formula blocks into sections by page proximity.

    Each FormulaBlock has a `page` field. Each Section has a `start_page` field.
    We assign each formula to the section whose page range contains the formula's page.
    A section's page range is [start_page, next_section.start_page).
    """
    if not formula_blocks or not doc.sections:
        return

    # Build page ranges for sections
    sections_with_pages = []
    for i, s in enumerate(doc.sections):
        if s.start_page >= 0:
            sections_with_pages.append((s, s.start_page))

    if not sections_with_pages:
        # No page info — fall back to even distribution
        body_sections = [s for s in doc.sections if s.body.strip()]
        if body_sections:
            for i, fb in enumerate(formula_blocks):
                idx = i % len(body_sections)
                body_sections[idx].formula_blocks.append(fb)
        return

    # Sort formula blocks by page, then y-position
    formula_blocks.sort(key=lambda fb: (fb.page, fb.bbox_y))

    for fb in formula_blocks:
        best_section = None
        best_distance = float("inf")

        for i, (section, page) in enumerate(sections_with_pages):
            # Compute next section's start page (for range)
            if i + 1 < len(sections_with_pages):
                next_page = sections_with_pages[i + 1][1]
            else:
                next_page = float("inf")

            # Formula is within this section's page range
            if page <= fb.page < next_page:
                best_section = section
                break

            # Otherwise find closest by page distance
            dist = abs(fb.page - page)
            if dist < best_distance:
                best_distance = dist
                best_section = section

        if best_section:
            best_section.formula_blocks.append(fb)


def _attach_figures_to_sections(doc: Document, figures: list):
    """
    Distribute extracted figures across sections.
    Simple strategy: spread evenly across sections that have body text.
    On Windows with fitz, figures come with page numbers so we can do
    page-proximity matching in a future improvement.
    """
    if not figures or not doc.sections:
        return

    body_sections = [s for s in doc.sections if s.body.strip()]
    if not body_sections:
        return

    for i, fig in enumerate(figures):
        idx = i % len(body_sections)
        body_sections[idx].figures.append(fig)


def _flush_log(logger):
    """Force-flush all log handlers so nothing is lost if the process crashes."""
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass


def _save_structured_json(doc: Document):
    """Dump Document to intermediate/structured.json for debugging."""
    from dataclasses import asdict
    path = os.path.join(INTERMEDIATE_DIR, "structured.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(doc), f, indent=2, ensure_ascii=False, default=str)
