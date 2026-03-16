"""
stages/normalizer.py
Stage 3 — Content Normalization

Responsibility:
  - Fix PDF extraction artifacts (ligatures, hyphenation, encoding)
  - Normalise whitespace
  - Return a cleaned copy of the Document (does not mutate the original)

Add more rules here as you encounter edge cases.
"""

import re
from copy import deepcopy
from core.models import Document, Section, Author


# ── Ligature & Unicode fixes ──────────────────────────────────────────────────
# PDF fonts often bake ligatures into single code points that don't round-trip.
_LIGATURES: dict[str, str] = {
    "ﬁ": "fi",  "ﬂ": "fl",  "ﬀ": "ff",
    "ﬃ": "ffi", "ﬄ": "ffl",
    "\ufb01": "fi", "\ufb02": "fl",
    # Smart quotes → LaTeX-friendly
    "\u2018": "`",  "\u2019": "'",
    "\u201c": "``", "\u201d": "''",
    # Dashes → LaTeX en/em dash
    "\u2013": "--", "\u2014": "---",
}


def normalize(doc: Document) -> Document:
    """Return a cleaned deep-copy of doc."""
    doc = deepcopy(doc)

    doc.title     = _clean(doc.title)
    doc.abstract  = _clean(doc.abstract)
    doc.authors   = [
        Author(
            name         = _clean(a.name),
            department   = _clean(a.department),
            organization = _clean(a.organization),
            city         = _clean(a.city),
            country      = _clean(a.country),
            email        = _clean(a.email),
        )
        for a in doc.authors
    ]
    doc.keywords  = [_clean(k) for k in doc.keywords]
    doc.references = [_clean(r) for r in doc.references]
    doc.sections  = [
        Section(heading=_clean(s.heading), body=_clean(s.body))
        for s in doc.sections
    ]

    return doc


# ── Text cleaning ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    if not text:
        return text
    text = _fix_ligatures(text)
    text = _fix_hyphenation(text)
    text = _fix_whitespace(text)
    return text.strip()


def _fix_ligatures(text: str) -> str:
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    return text


def _fix_hyphenation(text: str) -> str:
    """Remove soft hyphens inserted at line-breaks during PDF extraction."""
    return re.sub(r"-\s*\n\s*", "", text)


def _fix_whitespace(text: str) -> str:
    text = re.sub(r"\r\n|\r", "\n", text)      # normalise line endings
    text = re.sub(r"[ \t]+", " ", text)         # collapse inline spaces
    text = re.sub(r"\n{3,}", "\n\n", text)      # max two newlines
    return text