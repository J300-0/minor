"""
canon/builder.py — Stage 2.5: Canonical Structure Builder

Takes the raw Document from the parser and produces a CanonicalDocument:
  1. VALIDATE  — check each field against constraints
  2. REPAIR    — apply fallback chains for missing/malformed fields
  3. SCORE     — assign confidence to each field
  4. GATE      — expose is_renderable() so bad docs never reach Jinja2

Repair chain: primary_parse -> fallback_1 -> fallback_2 -> default
Every fallback is logged.

Insert between Stage 3 (Normalize) and Stage 4 (Render) in pipeline.py.
"""
import logging
import re
from typing import Optional

from core.models import Document, Author, Section, Reference
from canon.models import CanonicalDocument, FieldResult
from canon.features import extract_features, title_score

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def build_canonical(doc: Document) -> CanonicalDocument:
    """
    Main entry point.  Call after Stage 3 (Normalize), before Stage 4 (Render).
    Returns a CanonicalDocument.  Check .is_renderable() before rendering.
    """
    builder = _CanonicalBuilder(doc)
    return builder.build()


# ══════════════════════════════════════════════════════════════════════════════
# Internal builder class
# ══════════════════════════════════════════════════════════════════════════════

class _CanonicalBuilder:

    def __init__(self, doc: Document):
        self.doc = doc
        self.canon = CanonicalDocument()
        self._log = []

    # ── Main build ─────────────────────────────────────────────────────────

    def build(self) -> CanonicalDocument:
        self.canon.title      = self._build_title()
        self.canon.authors    = self._build_authors()
        self.canon.abstract   = self._build_abstract()
        self.canon.keywords   = self._build_keywords()
        self.canon.sections   = self._build_sections()
        self.canon.references = self._build_references()
        self.canon.repair_log = self._log

        self._cross_validate()

        log.info("Canonical build complete:\n%s", self.canon.summary())
        return self.canon

    # ── Title ──────────────────────────────────────────────────────────────

    def _build_title(self) -> FieldResult:
        raw = (self.doc.title or "").strip()

        if raw and self._is_plausible_title(raw):
            return FieldResult(raw, confidence=0.9, source="parsed")

        # Fallback 1: first section heading
        if self.doc.sections:
            first_head = (self.doc.sections[0].heading or "").strip()
            if first_head and self._is_plausible_title(first_head):
                self._repair(f"title: used first section heading {first_head!r}")
                return FieldResult(first_head, confidence=0.5,
                                   source="repaired:first_section_heading")

        # Fallback 2: raw title with low confidence
        if raw:
            self._repair(f"title: using raw parsed value with low confidence: {raw!r}")
            return FieldResult(raw, confidence=0.3, source="repaired:raw_low_confidence")

        # Fallback 3: body scan
        candidate = self._score_best_title_from_body()
        if candidate:
            self._repair(f"title: extracted from body text: {candidate!r}")
            return FieldResult(candidate, confidence=0.25, source="repaired:body_scan")

        # Last resort
        self._repair("title: no title found, using placeholder")
        self.canon.warnings.append("title could not be extracted — placeholder used")
        return FieldResult("Untitled Paper", confidence=0.0, source="default")

    def _is_plausible_title(self, text: str) -> bool:
        words = text.split()
        if not (2 <= len(words) <= 25):
            return False
        if "@" in text or "http" in text.lower():
            return False
        alpha = sum(1 for c in text if c.isalpha())
        if alpha / max(len(text), 1) < 0.5:
            return False
        return True

    def _score_best_title_from_body(self) -> Optional[str]:
        if not self.doc.sections:
            return None
        lines = (self.doc.sections[0].body or "").splitlines()
        best_score, best_line = 0.0, None
        for line in lines[:15]:
            line = line.strip()
            if not line:
                continue
            feats = extract_features(line)
            s = title_score(feats)
            if s > best_score:
                best_score, best_line = s, line
        return best_line if best_score > 0.3 else None

    # ── Authors ────────────────────────────────────────────────────────────

    def _build_authors(self) -> FieldResult:
        authors = self.doc.authors or []
        valid = []

        for a in authors:
            name = (a.name or "").strip()
            if not name:
                continue
            if self._is_bad_author_name(name):
                self._repair(f"authors: rejected bad name {name!r}")
                continue
            valid.append(a)

        if valid:
            no_affil = [a.name for a in valid if not (a.organization or "").strip()]
            if no_affil:
                self.canon.warnings.append(
                    f"authors with no affiliation: {no_affil}"
                )
            conf = 0.9 if len(valid) == len(authors) else 0.6
            return FieldResult(valid, confidence=conf, source="parsed")

        self._repair("authors: no valid authors found, using empty list")
        self.canon.warnings.append("no authors could be extracted")
        return FieldResult([], confidence=0.0, source="default")

    def _is_bad_author_name(self, name: str) -> bool:
        """Reject names that are obviously not real person names."""
        words = name.split()
        if len(words) < 2:
            return True
        # Reject names starting with articles: "The IAU", "A Method"
        if words[0].lower() in {"the", "a", "an"}:
            return True
        # Reject if any word is a common non-name word
        _BAD_WORDS = {
            "university", "institute", "department", "faculty", "journal",
            "conference", "technical", "proceedings", "international",
            "system", "method", "analysis", "review", "research",
        }
        if any(w.lower() in _BAD_WORDS for w in words):
            return True
        # Reject if name is too short (e.g. "J K")
        if len(name.replace(" ", "")) < 4:
            return True
        # Reject all-caps acronym pairs like "IEEE ACM"
        if all(w.isupper() and len(w) <= 5 for w in words):
            return True
        return False

    # ── Abstract ───────────────────────────────────────────────────────────

    def _build_abstract(self) -> FieldResult:
        raw = (self.doc.abstract or "").strip()

        if raw and len(raw) > 50:
            if len(raw) > 3000:
                self._repair(
                    f"abstract: unusually long ({len(raw)} chars), may contain body text"
                )
                self.canon.warnings.append(
                    "abstract is unusually long — may include body text"
                )
                return FieldResult(raw, confidence=0.5, source="parsed:long_warning")
            return FieldResult(raw, confidence=0.85, source="parsed")

        # Fallback: "Abstract" section in sections list
        for sec in (self.doc.sections or []):
            if "abstract" in (sec.heading or "").lower():
                body = (sec.body or "").strip()
                if len(body) > 50:
                    self._repair(
                        "abstract: found in sections list, not in doc.abstract field"
                    )
                    return FieldResult(body, confidence=0.7,
                                       source="repaired:found_in_sections")

        # Fallback 2: first body paragraph
        if not raw and self.doc.sections:
            first_body = (self.doc.sections[0].body or "").strip()
            if len(first_body) > 50:
                self._repair("abstract: using first section body as fallback")
                self.canon.warnings.append(
                    "abstract not found — first section body used"
                )
                return FieldResult(first_body[:1000], confidence=0.2,
                                   source="repaired:first_body")

        if raw:
            self._repair(f"abstract: very short ({len(raw)} chars)")
            return FieldResult(raw, confidence=0.3, source="parsed:short_warning")

        self._repair("abstract: no abstract found")
        return FieldResult("", confidence=0.0, source="default")

    # ── Keywords ───────────────────────────────────────────────────────────

    def _build_keywords(self) -> FieldResult:
        kws = self.doc.keywords or []
        cleaned = [k.strip() for k in kws if k and k.strip()]
        cleaned = [k for k in cleaned if 1 < len(k) < 80]

        if cleaned:
            return FieldResult(cleaned, confidence=0.85, source="parsed")

        # Fallback: regex in abstract
        abstract = self.doc.abstract or ""
        kw_match = re.search(r"[Kk]eywords?\s*[:\-]\s*(.+?)(?:\n|$)", abstract)
        if kw_match:
            kw_list = [
                k.strip() for k in re.split(r"[;,]", kw_match.group(1)) if k.strip()
            ]
            if kw_list:
                self._repair("keywords: extracted from abstract text")
                return FieldResult(kw_list, confidence=0.6,
                                   source="repaired:from_abstract")

        self._repair("keywords: none found")
        return FieldResult([], confidence=0.0, source="default")

    # ── Sections ───────────────────────────────────────────────────────────

    def _build_sections(self) -> FieldResult:
        sections = self.doc.sections or []
        valid = []

        for sec in sections:
            body = (sec.body or "").strip()
            heading = (sec.heading or "").strip()

            if self._is_junk_section(heading, body):
                self._repair(f"sections: dropped junk section {heading!r}")
                continue

            if not body and heading:
                self.canon.warnings.append(f"section {heading!r} has empty body")

            valid.append(sec)

        if not valid:
            self._repair("sections: no valid sections found")
            self.canon.warnings.append(
                "CRITICAL: no sections extracted — document will be empty"
            )
            return FieldResult([], confidence=0.0, source="default")

        conf = 0.9
        if len(valid) < 3:
            conf = 0.5
            self.canon.warnings.append(
                f"only {len(valid)} sections found — extraction may be incomplete"
            )
        long_heads = [s.heading for s in valid if len(s.heading or "") > 80]
        if long_heads:
            conf = min(conf, 0.4)
            self._repair(
                f"sections: {len(long_heads)} sections have very long headings"
            )

        src = "parsed" if conf >= 0.8 else "parsed:low_confidence"
        return FieldResult(valid, confidence=conf, source=src)

    def _is_junk_section(self, heading: str, body: str) -> bool:
        if not heading and not body:
            return True
        h = heading.lower()
        if re.match(r"^[A-Z]{2,6}$", heading) and len(body) < 20:
            return True
        boilerplate = [
            "springer nature", "open access", "authors and affiliations",
            "creative commons", "cc by",
        ]
        if any(bp in h for bp in boilerplate):
            return True
        return False

    # ── References ─────────────────────────────────────────────────────────

    def _build_references(self) -> FieldResult:
        refs = self.doc.references or []
        valid = []

        for ref in refs:
            text = (ref.text or "").strip()
            if not text or len(text) < 10:
                continue
            if any(bp in text.lower()
                   for bp in ["springer nature", "open access", "cc by"]):
                self._repair(f"refs: dropped boilerplate ref: {text[:60]!r}")
                continue
            valid.append(ref)

        if valid:
            conf = 0.9 if len(valid) >= 5 else 0.5
            return FieldResult(valid, confidence=conf, source="parsed")

        self._repair("refs: no valid references found")
        self.canon.warnings.append("no references extracted")
        return FieldResult([], confidence=0.0, source="default")

    # ── Cross-validation ───────────────────────────────────────────────────

    def _cross_validate(self):
        title_val = self.canon.title.value or ""
        sections = self.canon.sections.value or []

        # Title should not equal a section heading
        for sec in sections:
            if (sec.heading or "").strip().lower() == title_val.lower():
                self.canon.warnings.append(
                    f"title is identical to section heading {sec.heading!r}"
                )
                self.canon.title = FieldResult(
                    title_val,
                    confidence=min(self.canon.title.confidence, 0.3),
                    source=self.canon.title.source + ":title_heading_collision",
                )
                break

        # Abstract shouldn't start with a section keyword
        abstract_val = self.canon.abstract.value or ""
        if abstract_val:
            first_word = abstract_val.split()[0].lower() if abstract_val.split() else ""
            section_kws = {
                "introduction", "methodology", "method", "results",
                "conclusion", "background", "related",
            }
            if first_word in section_kws:
                self.canon.warnings.append(
                    "abstract starts with a section keyword — may contain body text"
                )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _repair(self, msg: str):
        self._log.append(msg)
        log.debug("[CANON REPAIR] %s", msg)
