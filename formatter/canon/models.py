"""
canon/models.py — CanonicalDocument and FieldResult.

Every field has a value, confidence (0.0–1.0), and source tag.
"""
from dataclasses import dataclass, field
from typing import Any, List

from core.models import Document, Author, Section, Reference


@dataclass
class FieldResult:
    value: Any = None
    confidence: float = 0.0
    source: str = "default"  # "parsed", "repaired:fallback_name", "default"


@dataclass
class CanonicalDocument:
    title: FieldResult = field(default_factory=FieldResult)
    authors: FieldResult = field(default_factory=FieldResult)
    abstract: FieldResult = field(default_factory=FieldResult)
    keywords: FieldResult = field(default_factory=FieldResult)
    sections: FieldResult = field(default_factory=FieldResult)
    references: FieldResult = field(default_factory=FieldResult)
    formula_blocks: FieldResult = field(default_factory=FieldResult)
    repair_log: List[str] = field(default_factory=list)

    def is_renderable(self) -> bool:
        """
        Returns True only if:
        1. title exists (confidence > 0)
        2. at least 1 section with non-empty body
        3. no critical field is completely missing
        """
        # Must have a title
        if not self.title.value or self.title.confidence <= 0:
            return False

        # Must have at least one section with body text
        sections = self.sections.value or []
        if not any(s.body and s.body.strip() for s in sections if hasattr(s, "body")):
            return False

        return True

    def summary(self) -> str:
        """One-line summary of the canonical document state."""
        sections = self.sections.value or []
        refs = self.references.value or []
        parts = [
            f"title={self.title.confidence:.1f}({self.title.source})",
            f"authors={self.authors.confidence:.1f}({len(self.authors.value or [])})",
            f"abstract={self.abstract.confidence:.1f}({len(self.abstract.value or '')} chars)",
            f"sections={self.sections.confidence:.1f}({len(sections)})",
            f"refs={self.references.confidence:.1f}({len(refs)})",
        ]
        if self.repair_log:
            parts.append(f"repairs={len(self.repair_log)}")
        return " | ".join(parts)

    def to_document(self) -> Document:
        """Unwrap back to a plain Document for Jinja2 rendering."""
        return Document(
            title=self.title.value or "",
            authors=self.authors.value or [],
            abstract=self.abstract.value or "",
            keywords=self.keywords.value or [],
            sections=self.sections.value or [],
            references=self.references.value or [],
            formula_blocks=(self.formula_blocks.value or []),
        )
