"""
canon/ — Canonical Structure Builder

Stage 2.5 in the pipeline: validate, repair, and score a parsed Document
before it reaches the Jinja2 renderer.

Usage:
    from canon.builder import build_canonical
    canon_doc = build_canonical(doc)
    if not canon_doc.is_renderable():
        log.error("Document not renderable:\n%s", canon_doc.summary())
        raise PipelineError("Canonical check failed")
    doc = canon_doc.to_document()   # unwrap for renderer
"""
from canon.builder import build_canonical
from canon.models import CanonicalDocument, FieldResult

__all__ = ["build_canonical", "CanonicalDocument", "FieldResult"]