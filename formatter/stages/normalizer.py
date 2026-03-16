"""
stages/normalizer.py
Stage 3 — Content Normalization

Responsibility:
  - Fix PDF extraction artifacts (ligatures, hyphenation, encoding)
  - Replace Unicode math symbols with LaTeX equivalents (π → $\pi$)
  - Normalise whitespace
  - Return a cleaned copy of the Document (does not mutate the original)

Add more rules here as you encounter edge cases.
"""

import re
from copy import deepcopy
from core.models import Document, Section, Author


# ── Ligature & Unicode fixes ──────────────────────────────────────────────────
_LIGATURES: dict[str, str] = {
    "ﬁ": "fi",  "ﬂ": "fl",  "ﬀ": "ff",
    "ﬃ": "ffi", "ﬄ": "ffl",
    "\ufb01": "fi", "\ufb02": "fl",
    # Smart quotes → LaTeX-friendly
    "\u2018": "`",  "\u2019": "'",
    "\u201c": "``", "\u201d": "''",
    # Dashes
    "\u2013": "--", "\u2014": "---",
}

# ── Unicode math symbols → LaTeX ──────────────────────────────────────────────
# Handles Unicode math chars that appear in plain body text from PDFs/DOCX.
# These crash pdflatex if passed raw — replace them before the tex stage.
# Note: content already inside $...$ or %%RAWTEX%% is NOT processed here
# because _clean() is called on plain text fields, not raw LaTeX blocks.
_MATH_SYMBOLS: dict[str, str] = {
    "π": r"$\pi$",       "α": r"$\alpha$",    "β": r"$\beta$",
    "γ": r"$\gamma$",    "δ": r"$\delta$",    "ε": r"$\epsilon$",
    "ζ": r"$\zeta$",     "η": r"$\eta$",      "θ": r"$\theta$",
    "λ": r"$\lambda$",   "μ": r"$\mu$",       "ν": r"$\nu$",
    "ξ": r"$\xi$",       "ρ": r"$\rho$",      "σ": r"$\sigma$",
    "τ": r"$\tau$",      "φ": r"$\phi$",      "χ": r"$\chi$",
    "ψ": r"$\psi$",      "ω": r"$\omega$",
    "Γ": r"$\Gamma$",    "Δ": r"$\Delta$",    "Θ": r"$\Theta$",
    "Λ": r"$\Lambda$",   "Σ": r"$\Sigma$",    "Φ": r"$\Phi$",
    "Ψ": r"$\Psi$",      "Ω": r"$\Omega$",
    "∞": r"$\infty$",    "∑": r"$\sum$",      "∏": r"$\prod$",
    "∫": r"$\int$",      "√": r"$\sqrt{}$",   "∂": r"$\partial$",
    "≤": r"$\leq$",      "≥": r"$\geq$",      "≠": r"$\neq$",
    "≈": r"$\approx$",   "±": r"$\pm$",       "×": r"$\times$",
    "÷": r"$\div$",      "·": r"$\cdot$",     "°": r"${}^{\circ}$",
    "⊗": r"$\otimes$",   "⊕": r"$\oplus$",   "∈": r"$\in$",
    "∉": r"$\notin$",    "⊆": r"$\subseteq$", "⊂": r"$\subset$",
    "∪": r"$\cup$",      "∩": r"$\cap$",      "∅": r"$\emptyset$",
    "→": r"$\rightarrow$","←": r"$\leftarrow$","↔": r"$\leftrightarrow$",
    "⇒": r"$\Rightarrow$","⇔": r"$\Leftrightarrow$",
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
        Section(heading=_clean(s.heading), body=_clean_body(s.body))
        for s in doc.sections
    ]

    return doc


# ── Text cleaning ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    if not text:
        return text
    text = _fix_ligatures(text)
    text = _fix_math_symbols(text)
    text = _fix_hyphenation(text)
    text = _fix_whitespace(text)
    return text.strip()


def _clean_body(text: str) -> str:
    """
    Clean section body text. Skips %%RAWTEX%%...%%ENDRAWTEX%% blocks
    so we don't accidentally process already-valid LaTeX.
    """
    if not text:
        return text
    parts = re.split(r"(%%RAWTEX%%.*?%%ENDRAWTEX%%)", text, flags=re.DOTALL)
    cleaned = []
    for part in parts:
        if part.startswith("%%RAWTEX%%"):
            cleaned.append(part)  # pass raw LaTeX through untouched
        else:
            cleaned.append(_clean(part))
    return "".join(cleaned)


def _fix_ligatures(text: str) -> str:
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    return text


def _fix_math_symbols(text: str) -> str:
    """Replace bare Unicode math symbols with LaTeX equivalents."""
    for sym, latex in _MATH_SYMBOLS.items():
        text = text.replace(sym, latex)
    return text


def _fix_hyphenation(text: str) -> str:
    return re.sub(r"-\s*\n\s*", "", text)


def _fix_whitespace(text: str) -> str:
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text