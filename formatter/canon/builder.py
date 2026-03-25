"""
canon/builder.py — Validate + repair + score + cross-validate.

Each field has a fallback chain. If primary parse fails, next fallback is tried.
"""
import re
import logging
from typing import List

from core.models import Document, Section
from canon.models import CanonicalDocument, FieldResult

log = logging.getLogger("paper_formatter")


def build_canonical(doc: Document) -> CanonicalDocument:
    """
    Takes a parsed Document, validates every field, repairs where possible,
    and returns a CanonicalDocument with confidence scores.
    """
    builder = _CanonicalBuilder(doc)
    return builder.build()


class _CanonicalBuilder:
    def __init__(self, doc: Document):
        self.doc = doc
        self.repair_log = []

    def build(self) -> CanonicalDocument:
        canon = CanonicalDocument()
        canon.title = self._validate_title()
        canon.authors = self._validate_authors()
        canon.abstract = self._validate_abstract()
        canon.keywords = self._validate_keywords()
        canon.sections = self._validate_sections()
        canon.references = self._validate_references()
        canon.formula_blocks = self._validate_formulas()
        canon.repair_log = self.repair_log
        return canon

    # ── Title ─────────────────────────────────────────────────

    def _validate_title(self) -> FieldResult:
        title = (self.doc.title or "").strip()

        # Primary: parsed title
        if title and len(title) >= 5 and self._is_plausible_title(title):
            return FieldResult(value=title, confidence=0.9, source="parsed")

        # Fallback 1: first section heading
        for s in self.doc.sections:
            if s.heading and self._is_plausible_title(s.heading):
                self._log("title: repaired from first section heading")
                return FieldResult(value=s.heading, confidence=0.6,
                                   source="repaired:first_heading")

        # Fallback 2: scan body for title-like text
        for s in self.doc.sections:
            if s.body:
                first_line = s.body.split("\n")[0].strip()
                if 10 < len(first_line) < 200:
                    self._log("title: repaired from body scan")
                    return FieldResult(value=first_line, confidence=0.4,
                                       source="repaired:body_scan")

        # Default
        if title:
            return FieldResult(value=title, confidence=0.3, source="parsed:weak")

        self._log("title: using default placeholder")
        return FieldResult(value="Untitled Paper", confidence=0.0, source="default")

    def _is_plausible_title(self, text: str) -> bool:
        """A plausible title has 2+ words and isn't metadata."""
        words = text.split()
        if len(words) < 2:
            return False
        if len(text) > 300:
            return False
        # Reject if it's a URL or DOI
        if text.startswith("http") or text.startswith("doi:"):
            return False
        return True

    # ── Authors ───────────────────────────────────────────────

    def _validate_authors(self) -> FieldResult:
        authors = self.doc.authors or []

        # Filter bad author names
        good_authors = [a for a in authors if not self._is_bad_author_name(a.name)]

        if good_authors:
            dropped = len(authors) - len(good_authors)
            if dropped > 0:
                self._log(f"authors: dropped {dropped} bad names")
            return FieldResult(value=good_authors, confidence=0.8, source="parsed")

        self._log("authors: none found")
        return FieldResult(value=[], confidence=0.0, source="default")

    def _is_bad_author_name(self, name: str) -> bool:
        """Reject names that are clearly not author names."""
        name = name.strip()
        if not name:
            return True
        lower = name.lower()
        # Starts with articles
        if lower.startswith(("the ", "a ", "an ")):
            return True
        # Contains affiliation keywords
        bad_words = ["university", "institute", "department", "ieee", "acm",
                     "conference", "journal", "proceedings"]
        if any(w in lower for w in bad_words):
            return True
        # Very short
        if len(name) < 3:
            return True
        # All-caps acronym pairs
        if re.match(r"^[A-Z]{2,}\s+[A-Z]{2,}$", name):
            return True
        return False

    # ── Abstract ──────────────────────────────────────────────

    def _validate_abstract(self) -> FieldResult:
        abstract = (self.doc.abstract or "").strip()

        if abstract and len(abstract) > 50:
            return FieldResult(value=abstract, confidence=0.9, source="parsed")

        if abstract:
            return FieldResult(value=abstract, confidence=0.5, source="parsed:short")

        # Fallback 1: look for "Abstract" section in section list
        for s in self.doc.sections:
            if s.heading.lower() == "abstract" and s.body:
                self._log("abstract: repaired from sections list")
                return FieldResult(value=s.body.strip(), confidence=0.7,
                                   source="repaired:section")

        # Fallback 2: first paragraph of body
        for s in self.doc.sections:
            if s.body and len(s.body.strip()) > 100:
                first_para = s.body.strip().split("\n\n")[0]
                self._log("abstract: repaired from first body paragraph")
                return FieldResult(value=first_para, confidence=0.3,
                                   source="repaired:first_para")

        self._log("abstract: none found")
        return FieldResult(value="", confidence=0.0, source="default")

    # ── Keywords ──────────────────────────────────────────────

    def _validate_keywords(self) -> FieldResult:
        keywords = self.doc.keywords or []

        if keywords:
            return FieldResult(value=keywords, confidence=0.9, source="parsed")

        # Fallback: try to extract from abstract text
        abstract = self.doc.abstract or ""
        if abstract:
            # Look for "Keywords:" embedded in abstract
            m = re.search(r"(?:keywords|key\s*words)[\s:—-]+(.+)", abstract, re.I)
            if m:
                kws = re.split(r"[;,·•]", m.group(1))
                kws = [k.strip().rstrip(".") for k in kws if k.strip()]
                if kws:
                    self._log("keywords: repaired from abstract text")
                    return FieldResult(value=kws, confidence=0.5,
                                       source="repaired:abstract")

        return FieldResult(value=[], confidence=0.0, source="default")

    # ── Sections ──────────────────────────────────────────────

    def _validate_sections(self) -> FieldResult:
        sections = self.doc.sections or []

        # Drop junk sections (empty body, or heading is metadata)
        good = []
        for s in sections:
            if not s.body.strip():
                self._log(f"sections: dropped empty section '{s.heading}'")
                continue
            if self._is_metadata_heading(s.heading):
                self._log(f"sections: dropped metadata section '{s.heading}'")
                continue
            good.append(s)

        if good:
            return FieldResult(value=good, confidence=0.8, source="parsed")

        self._log("sections: none found")
        return FieldResult(value=[], confidence=0.0, source="default")

    def _is_metadata_heading(self, heading: str) -> bool:
        """Check if a heading is actually metadata."""
        lower = heading.lower().strip()
        metadata_words = ["doi:", "issn", "copyright", "authorized", "proceedings of"]
        return any(w in lower for w in metadata_words)

    # ── References ────────────────────────────────────────────

    def _validate_references(self) -> FieldResult:
        refs = self.doc.references or []

        if refs:
            # Drop boilerplate entries
            good = [r for r in refs if not self._is_boilerplate_ref(r.text)]
            dropped = len(refs) - len(good)
            if dropped > 0:
                self._log(f"references: dropped {dropped} boilerplate entries")
            return FieldResult(value=good,
                               confidence=0.8 if good else 0.0,
                               source="parsed")

        return FieldResult(value=[], confidence=0.0, source="default")

    def _is_boilerplate_ref(self, text: str) -> bool:
        """Check if reference text is publisher boilerplate."""
        lower = text.lower()
        boilerplate = ["authorized licensed use", "downloaded on",
                       "restrictions apply", "personal use is permitted"]
        return any(b in lower for b in boilerplate)

    # ── Formula blocks ────────────────────────────────────────

    def _validate_formulas(self) -> FieldResult:
        fbs = self.doc.formula_blocks or []
        if fbs:
            return FieldResult(value=fbs, confidence=0.8, source="parsed")
        return FieldResult(value=[], confidence=0.0, source="default")

    # ── Logging ───────────────────────────────────────────────

    def _log(self, msg: str):
        self.repair_log.append(msg)
        log.debug("  Canon: %s", msg)
