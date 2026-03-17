"""
ai/cleaning_llm.py  —  Stage 3: Document normalisation (ligatures, Unicode, math)

Does NOT call the LLM — runs fast local text transformations.
"""

import re
from copy import deepcopy
from core.models import Document, Section, Author, Reference

_LIGATURES = {
    "ﬁ":"fi","ﬂ":"fl","ﬀ":"ff","ﬃ":"ffi","ﬄ":"ffl",
    "\ufb01":"fi","\ufb02":"fl",
    "\u2018":"`","\u2019":"'","\u201c":"``","\u201d":"''",
    "\u2013":"--","\u2014":"---",
}
_MATH = {
    "π":r"$\pi$","α":r"$\alpha$","β":r"$\beta$","γ":r"$\gamma$","δ":r"$\delta$",
    "ε":r"$\epsilon$","θ":r"$\theta$","λ":r"$\lambda$","μ":r"$\mu$","σ":r"$\sigma$",
    "τ":r"$\tau$","φ":r"$\phi$","ω":r"$\omega$","ϕ":r"$\phi$","ϵ":r"$\epsilon$",
    "Σ":r"$\Sigma$","Γ":r"$\Gamma$","Δ":r"$\Delta$","Λ":r"$\Lambda$","Ω":r"$\Omega$",
    "∞":r"$\infty$","∑":r"$\sum$","∫":r"$\int$","∂":r"$\partial$",
    "≤":r"$\leq$","≥":r"$\geq$","≠":r"$\neq$","≈":r"$\approx$","±":r"$\pm$",
    "×":r"$\times$","÷":r"$\div$","·":r"$\cdot$",
    "→":r"$\rightarrow$","←":r"$\leftarrow$","↔":r"$\leftrightarrow$",
    "⇒":r"$\Rightarrow$","⇔":r"$\Leftrightarrow$","⇑":r"$\Uparrow$","⇓":r"$\Downarrow$",
    "∈":r"$\in$","∉":r"$\notin$","⊆":r"$\subseteq$","∪":r"$\cup$","∩":r"$\cap$",
    "∅":r"$\emptyset$","∀":r"$\forall$","∃":r"$\exists$",
    "′":"'","∗":r"$*$","∼":r"$\sim$","⊤":r"$\top$","−":"-",
    "●":r"$\bullet$","□":r"$\square$","⋆":r"$\star$","̸":"",
}
_UNICODE_SPACES = {"\u00a0","\u2002","\u2003","\u2004","\u2005",
                   "\u2006","\u2007","\u2008","\u2009","\u200a","\u202f","\u205f"}
_STRIP_CHARS   = {"\u0008","\u001b","\u001c","\u001d","\u001e","\u001f",
                  "\u200b","\uf8ee","\uf8ef","\uf8f0","\uf8f9","\uf8fa","\uf8fb"}


def normalize(doc: Document) -> Document:
    doc = deepcopy(doc)
    doc.title    = _clean(doc.title)
    doc.abstract = _clean(doc.abstract)
    doc.keywords = [_clean(k) for k in doc.keywords]
    doc.authors  = [Author(
        name=_clean(a.name), department=_clean(a.department),
        organization=_clean(a.organization), city=_clean(a.city),
        country=_clean(a.country), email=_clean(a.email),
    ) for a in doc.authors]
    # FIX: Reference objects must be cleaned field-by-field, not as strings
    doc.references = [Reference(index=r.index, text=_clean(r.text)) for r in doc.references]
    doc.sections = [Section(
        heading=_clean(s.heading),
        body=_clean_body(s.body),
        tables=s.tables,
        figures=s.figures,
    ) for s in doc.sections]
    return doc


def _clean(text: str) -> str:
    if not text:
        return text or ""
    # Safety: if somehow a non-string gets here, convert it
    if not isinstance(text, str):
        text = str(text)
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = re.sub(r"\\textbullet\s*", "", text)
    for ch in _UNICODE_SPACES: text = text.replace(ch, " ")
    for ch in _STRIP_CHARS:    text = text.replace(ch, "")
    for sym, lat in _MATH.items(): text = text.replace(sym, lat)
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_body(text: str) -> str:
    if not text:
        return ""
    parts = re.split(r"(%%RAWTEX%%.*?%%ENDRAWTEX%%)", text, flags=re.DOTALL)
    return "".join(p if p.startswith("%%RAWTEX%%") else _clean(p) for p in parts)