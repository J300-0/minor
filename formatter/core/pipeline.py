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
from core.shared import OCR_CONFIDENCE_THRESHOLD

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
        # Convert all dicts to dataclasses upfront — single type from here on
        formula_blocks = [
            FormulaBlock(**fb) if isinstance(fb, dict) else fb
            for fb in formula_blocks
        ]
        good_fbs = []
        for fb in formula_blocks:
            # Accept if: has good OCR (conf >= threshold), or has image fallback (no latex)
            if fb.latex and fb.confidence >= OCR_CONFIDENCE_THRESHOLD:
                good_fbs.append(fb)
            elif fb.image_path and not fb.latex:
                # Image-only fallback — keep it (rendered as \includegraphics)
                good_fbs.append(fb)
        kept = len(good_fbs)
        total = len(formula_blocks)
        log.info("  Formula blocks: %d kept / %d total (conf >= %.2f or image-only)", kept, total, OCR_CONFIDENCE_THRESHOLD)

        # Distribute formula blocks into sections by page proximity
        _distribute_to_sections(doc, good_fbs, "formula_blocks")

        # Force unplaced formulas into the last body section
        # (avoids a disconnected "Key Equations" section at the end)
        placed = set()
        for s in doc.sections:
            for fb in s.formula_blocks:
                placed.add(id(fb))
        unplaced = [fb for fb in good_fbs if id(fb) not in placed]
        if unplaced:
            body_sections = [s for s in doc.sections if s.body.strip()]
            target = body_sections[-1] if body_sections else doc.sections[-1]
            for fb in unplaced:
                target.formula_blocks.append(fb)
            log.info("  %d formula blocks placed in sections, %d forced into last section",
                     kept - len(unplaced), len(unplaced))
        doc.formula_blocks = []  # nothing left for global rendering

    # Attach extracted figures to sections (distribute by page proximity)
    raw_figures = raw.get("figures", [])
    if raw_figures:
        from core.models import Figure
        figures = [
            Figure(**fig) if isinstance(fig, dict) else fig
            for fig in raw_figures
        ]
        _distribute_to_sections(doc, figures, "figures")
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


def _distribute_to_sections(doc: Document, items: list, target_attr: str,
                             page_fn=None, sort_key=None):
    """
    Distribute items (formulas, figures, etc.) into sections by page proximity.

    Args:
        doc: Document with sections
        items: list of items to distribute (dataclass or dict objects)
        target_attr: section attribute name to append to (e.g. 'formula_blocks', 'figures')
        page_fn: callable(item) -> page number (default: item.page or item["page"])
        sort_key: callable for sorting items before distribution (default: by page, bbox_y)
    """
    if not items or not doc.sections:
        return

    # Default page accessor
    if page_fn is None:
        def page_fn(item):
            return item.page if hasattr(item, 'page') else item.get("page", -1)

    # Default sort key
    if sort_key is None:
        def sort_key(item):
            page = page_fn(item)
            y = item.bbox_y if hasattr(item, 'bbox_y') else item.get("bbox_y", 0)
            return (page, y)

    # Build page ranges for sections
    sections_with_pages = []
    for s in doc.sections:
        if s.start_page >= 0 and s.body.strip():
            sections_with_pages.append((s, s.start_page))

    body_sections = [s for s in doc.sections if s.body.strip()]
    if not body_sections:
        return

    # Check if we have page info
    has_page_info = sections_with_pages and any(page_fn(item) >= 0 for item in items)

    if not has_page_info:
        # No page info — fall back to even distribution
        for i, item in enumerate(items):
            idx = i % len(body_sections)
            getattr(body_sections[idx], target_attr).append(item)
        return

    # Sort items by position
    sorted_items = sorted(items, key=sort_key)

    for item in sorted_items:
        item_page = page_fn(item)

        if item_page < 0:
            # No page info — put in first section
            getattr(body_sections[0], target_attr).append(item)
            continue

        best_section = None
        best_distance = float("inf")

        for i, (section, page) in enumerate(sections_with_pages):
            if i + 1 < len(sections_with_pages):
                next_page = sections_with_pages[i + 1][1]
            else:
                next_page = float("inf")

            # Item is within this section's page range
            if page <= item_page < next_page:
                best_section = section
                break

            dist = abs(item_page - page)
            if dist < best_distance:
                best_distance = dist
                best_section = section

        if best_section:
            getattr(best_section, target_attr).append(item)
        else:
            getattr(body_sections[-1], target_attr).append(item)


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
