"""
canon/models.py — CanonicalDocument: validated Document with confidence scores + repair log.

Every field has:
  - The actual value (always a safe non-None type)
  - A confidence score 0.0–1.0
  - A source tag ("parsed" | "repaired_fallback_N" | "default")

The repair_log list records every fix made so you can diagnose issues from logs.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from core.models import Author, Section, Reference, Table, Figure


# ── Per-field confidence wrapper ──────────────────────────────────────────────

@dataclass
class FieldResult:
    """Wraps a single extracted field with its confidence and source."""
    value: object          # The actual value
    confidence: float      # 0.0 (guessed) → 1.0 (very certain)
    source: str            # "parsed" | "repaired:fallback_1" | "default"

    def __repr__(self):
        return f"FieldResult(conf={self.confidence:.2f}, src={self.source!r}, val={str(self.value)[:60]!r})"


# ── Canonical Document ────────────────────────────────────────────────────────

@dataclass
class CanonicalDocument:
    """
    A fully validated, repaired Document ready for rendering.

    Fields always have safe values — no None, no empty strings where
    content is expected.  Check is_renderable() before passing to Jinja2.
    """

    # Core content fields
    title:      FieldResult = None
    authors:    FieldResult = None   # value = List[Author]
    abstract:   FieldResult = None
    keywords:   FieldResult = None   # value = List[str]
    sections:   FieldResult = None   # value = List[Section]
    references: FieldResult = None   # value = List[Reference]

    # Diagnostics
    repair_log: List[str] = field(default_factory=list)
    warnings:   List[str] = field(default_factory=list)

    # Overall quality score (mean of all field confidences)
    @property
    def overall_confidence(self) -> float:
        scores = []
        for attr in ("title", "authors", "abstract", "keywords", "sections", "references"):
            fr: FieldResult = getattr(self, attr)
            if fr is not None:
                scores.append(fr.confidence)
        return round(sum(scores) / len(scores), 3) if scores else 0.0

    def is_renderable(self) -> bool:
        """
        Returns True only if the document meets the minimum bar for rendering.
        A document is renderable if:
          - title exists (confidence > 0)
          - at least 1 section with non-empty body
          - no critical field is completely missing
        """
        if self.title is None or not self.title.value:
            return False
        if self.sections is None or not self.sections.value:
            return False
        if not any(s.body.strip() for s in self.sections.value):
            return False
        return True

    def summary(self) -> str:
        """Human-readable summary for logging."""
        lines = [
            f"  overall_confidence : {self.overall_confidence}",
            f"  title              : [{self.title.confidence:.2f}] {str(self.title.value)[:70]!r}",
            f"  authors            : [{self.authors.confidence:.2f}] {len(self.authors.value)} authors",
            f"  abstract           : [{self.abstract.confidence:.2f}] {len(self.abstract.value)} chars",
            f"  keywords           : [{self.keywords.confidence:.2f}] {self.keywords.value}",
            f"  sections           : [{self.sections.confidence:.2f}] {len(self.sections.value)} sections",
            f"  references         : [{self.references.confidence:.2f}] {len(self.references.value)} refs",
            f"  renderable         : {self.is_renderable()}",
        ]
        if self.repair_log:
            lines.append(f"  repairs ({len(self.repair_log)}):")
            for r in self.repair_log:
                lines.append(f"    - {r}")
        if self.warnings:
            lines.append(f"  warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"    ⚠ {w}")
        return "\n".join(lines)

    def to_document(self):
        """
        Convert back to a core.models.Document for the renderer.
        The renderer doesn't know about CanonicalDocument — it still
        expects a plain Document.  This method unwraps all FieldResults.
        """
        from core.models import Document
        return Document(
            title=self.title.value or "",
            authors=self.authors.value or [],
            abstract=self.abstract.value or "",
            keywords=self.keywords.value or [],
            sections=self.sections.value or [],
            references=self.references.value or [],
        )