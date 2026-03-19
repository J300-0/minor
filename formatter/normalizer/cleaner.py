"""
normalizer/cleaner.py — Stage 3: Fix ligatures, unicode, math symbols, whitespace.
Pure local transforms — no external calls.
"""
import re, unicodedata
from copy import deepcopy
from core.models import Document, Section, Author, Reference, Table, Figure

_LIGATURES = {
    "\ufb01": "fi",  "\ufb02": "fl",
    "\ufb00": "ff",  "\ufb03": "ffi", "\ufb04": "ffl",
    "\u2018": "`",   "\u2019": "'",
    "\u201c": "``",  "\u201d": "''",
    "\u2013": "--",  "\u2014": "---",
    "\u00ad": "",    # soft hyphen
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen
    "\u2012": "-",   # figure dash
    "\u2015": "---", # horizontal bar
}

# Using explicit Unicode escapes to avoid encoding issues across platforms
_MATH = {
    # Greek lowercase
    "\u03b1": r"$\alpha$",   "\u03b2": r"$\beta$",    "\u03b3": r"$\gamma$",
    "\u03b4": r"$\delta$",   "\u03b5": r"$\epsilon$",  "\u03b6": r"$\zeta$",
    "\u03b7": r"$\eta$",     "\u03b8": r"$\theta$",    "\u03b9": r"$\iota$",
    "\u03ba": r"$\kappa$",   "\u03bb": r"$\lambda$",   "\u03bc": r"$\mu$",
    "\u03bd": r"$\nu$",      "\u03be": r"$\xi$",       "\u03c0": r"$\pi$",
    "\u03c1": r"$\rho$",     "\u03c3": r"$\sigma$",    "\u03c4": r"$\tau$",
    "\u03c5": r"$\upsilon$", "\u03c6": r"$\phi$",      "\u03c7": r"$\chi$",
    "\u03c8": r"$\psi$",     "\u03c9": r"$\omega$",
    # Greek uppercase
    "\u0393": r"$\Gamma$",   "\u0394": r"$\Delta$",    "\u0398": r"$\Theta$",
    "\u039b": r"$\Lambda$",  "\u039e": r"$\Xi$",       "\u03a0": r"$\Pi$",
    "\u03a3": r"$\Sigma$",   "\u03a6": r"$\Phi$",      "\u03a8": r"$\Psi$",
    "\u03a9": r"$\Omega$",
    # Math operators & relations
    "\u2212": "-",            # MINUS SIGN → ASCII hyphen
    "\u00b1": r"$\pm$",      "\u00d7": r"$\times$",    "\u00f7": r"$\div$",
    "\u00b7": r"$\cdot$",    "\u2219": r"$\cdot$",     # bullet operator
    "\u221e": r"$\infty$",   "\u2211": r"$\sum$",      "\u220f": r"$\prod$",
    "\u222b": r"$\int$",     "\u2202": r"$\partial$",
    "\u2264": r"$\leq$",     "\u2265": r"$\geq$",
    "\u2260": r"$\neq$",     "\u2248": r"$\approx$",   "\u2261": r"$\equiv$",
    "\u221d": r"$\propto$",
    # Arrows
    "\u2190": r"$\leftarrow$",    "\u2192": r"$\rightarrow$",
    "\u2191": r"$\uparrow$",      "\u2193": r"$\downarrow$",
    "\u2194": r"$\leftrightarrow$",
    "\u21d0": r"$\Leftarrow$",    "\u21d2": r"$\Rightarrow$",
    "\u21d4": r"$\Leftrightarrow$",
    # Set theory & logic
    "\u2208": r"$\in$",      "\u2209": r"$\notin$",
    "\u2282": r"$\subset$",  "\u2283": r"$\supset$",
    "\u2286": r"$\subseteq$","\u2287": r"$\supseteq$",
    "\u222a": r"$\cup$",     "\u2229": r"$\cap$",
    "\u2205": r"$\emptyset$",
    "\u2200": r"$\forall$",  "\u2203": r"$\exists$",
    "\u2227": r"$\wedge$",   "\u2228": r"$\vee$",
    "\u00ac": r"$\neg$",
    # Circled operators
    "\u2295": r"$\oplus$",   "\u2297": r"$\otimes$",   "\u2296": r"$\ominus$",
    "\u2299": r"$\odot$",
    # Misc math
    "\u221a": r"$\sqrt{}$",  "\u2207": r"$\nabla$",
    "\u00b0": r"$^\circ$",   "\u2032": r"$'$",         "\u2033": r"$''$",
    "\u2026": r"\ldots{}",   "\u2022": r"\textbullet{}",
    # Subscript/superscript digits (common in PDF extraction)
    "\u2070": r"$^{0}$",     "\u00b9": r"$^{1}$",      "\u00b2": r"$^{2}$",
    "\u00b3": r"$^{3}$",     "\u2074": r"$^{4}$",      "\u2075": r"$^{5}$",
    "\u2076": r"$^{6}$",     "\u2077": r"$^{7}$",      "\u2078": r"$^{8}$",
    "\u2079": r"$^{9}$",     "\u207a": r"$^{+}$",      "\u207b": r"$^{-}$",
    "\u2080": r"$_{0}$",     "\u2081": r"$_{1}$",      "\u2082": r"$_{2}$",
    "\u2083": r"$_{3}$",     "\u2084": r"$_{4}$",      "\u2085": r"$_{5}$",
    "\u2086": r"$_{6}$",     "\u2087": r"$_{7}$",      "\u2088": r"$_{8}$",
    "\u2089": r"$_{9}$",
    # Fullwidth variants (common in Asian-extracted PDFs)
    "\uff0d": "-",            # fullwidth hyphen-minus
    "\ufe63": "-",            # small hyphen-minus
}

_UNICODE_SPACES = {
    "\u00a0", "\u2002", "\u2003", "\u2004", "\u2005",
    "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",
    "\u202f", "\u205f",
}
_STRIP_CHARS = {
    "\u0008", "\u001b", "\u200b", "\uf8ee", "\uf8ef",
    "\uf8f0", "\uf8f9", "\uf8fa", "\uf8fb",
    "\ufeff",  # BOM / zero-width no-break space
    "\u200c", "\u200d",  # zero-width joiner/non-joiner
}

# Characters safe for pdflatex with T1/utf8 encoding (Latin accented letters)
_SAFE_RANGES = (
    (0x00C0, 0x024F),   # Latin Extended-A & B (accented: é, ö, ñ, etc.)
    (0x0020, 0x007E),   # Basic ASCII
)


def normalize(doc: Document) -> Document:
    doc = deepcopy(doc)
    doc.title    = _clean(doc.title)
    doc.abstract = _clean(doc.abstract)
    doc.keywords = [_clean(k) for k in doc.keywords]
    doc.authors  = [Author(
        name         = _clean(a.name),
        department   = _clean(a.department),
        organization = _clean(a.organization),
        city         = _clean(a.city),
        country      = _clean(a.country),
        email        = _clean(a.email),
    ) for a in doc.authors]
    doc.references = [Reference(index=r.index, text=_clean(r.text)) for r in doc.references]
    doc.sections   = [Section(
        heading = _clean(s.heading),
        body    = _clean(s.body),
        tables  = [Table(
            caption = _clean(t.caption),
            headers = [_clean(h) for h in t.headers],
            rows    = [[_clean(c) for c in row] for row in t.rows],
            notes   = _clean(t.notes),
        ) for t in s.tables],
        figures = [Figure(
            caption    = _clean(f.caption) if f.caption else "",
            image_path = f.image_path,
            label      = f.label,
        ) for f in s.figures],
    ) for s in doc.sections]
    return doc


def _clean(text: str) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    for ch in _UNICODE_SPACES:
        text = text.replace(ch, " ")
    for ch in _STRIP_CHARS:
        text = text.replace(ch, "")
    for sym, lat in _MATH.items():
        text = text.replace(sym, lat)
    # Soft hyphen + space at word boundary
    text = re.sub(r"(\w)\u00ad?\s*-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ── SAFETY NET: catch any remaining Unicode that pdflatex can't handle ──
    # After all explicit replacements, scan for non-ASCII chars that are NOT
    # in the safe range (Latin accented letters) and replace them.
    cleaned = []
    for ch in text:
        cp = ord(ch)
        if cp <= 0x7E:
            # Basic ASCII — always safe
            cleaned.append(ch)
        elif any(lo <= cp <= hi for lo, hi in _SAFE_RANGES):
            # Latin accented letters — safe with T1+utf8 encoding
            cleaned.append(ch)
        else:
            # Unknown Unicode — try to decompose, else drop
            decomposed = unicodedata.normalize("NFKD", ch)
            ascii_approx = decomposed.encode("ascii", "ignore").decode("ascii")
            if ascii_approx:
                cleaned.append(ascii_approx)
            else:
                # Last resort: drop the character (it would crash pdflatex)
                cleaned.append("?")
    text = "".join(cleaned)

    return text.strip()
