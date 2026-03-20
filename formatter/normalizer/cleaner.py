"""
normalizer/cleaner.py — Stage 3: Fix ligatures, unicode, math symbols, whitespace.
Pure local transforms — no external calls.

Formula handling strategy:
  PROBLEM: per-char substitution turns "f(x) = ax^2" into
           "f(x) = $\\alpha$x$^{2}$" — a chain of single-symbol $...$ fragments.
           Then latex_escape() corrupts them and pdflatex chokes.

  FIX: After per-char substitution, run _merge_adjacent_math() which collapses
       "$\\alpha$$\\beta$" -> "$\\alpha\\beta$" repeatedly until stable.
       For lines that are almost entirely math fragments, _consolidate_math_lines()
       strips all $ delimiters and wraps the whole line in one $...$

  RULE: We deliberately do NOT use \\[...\\] display math blocks — they require
  precise paragraph boundary placement in .tex which Jinja2 templating makes
  unreliable and causes "Display math should end with $$" fatal errors.
"""
import logging
import re
import unicodedata
from copy import deepcopy

from core.models import Document, Section, Author, Reference, Table, Figure

log = logging.getLogger(__name__)


# ── Ligature and quote replacements ──────────────────────────────────────────

_LIGATURES = {
    "\ufb01": "fi",  "\ufb02": "fl",
    "\ufb00": "ff",  "\ufb03": "ffi", "\ufb04": "ffl",
    "\u2018": "`",   "\u2019": "'",
    "\u201c": "``",  "\u201d": "''",
    "\u2013": "--",  "\u2014": "---",
    "\u00ad": "",    # soft hyphen
    "\u2010": "-",   "\u2011": "-",   "\u2012": "-",
    "\u2015": "---",
}

# ── Math symbols -> LaTeX command strings (WITHOUT $ wrappers) ───────────────

_MATH = {
    # Greek lowercase
    "\u03b1": r"\alpha",   "\u03b2": r"\beta",    "\u03b3": r"\gamma",
    "\u03b4": r"\delta",   "\u03b5": r"\epsilon",  "\u03b6": r"\zeta",
    "\u03b7": r"\eta",     "\u03b8": r"\theta",    "\u03b9": r"\iota",
    "\u03ba": r"\kappa",   "\u03bb": r"\lambda",   "\u03bc": r"\mu",
    "\u03bd": r"\nu",      "\u03be": r"\xi",       "\u03c0": r"\pi",
    "\u03c1": r"\rho",     "\u03c3": r"\sigma",    "\u03c4": r"\tau",
    "\u03c5": r"\upsilon", "\u03c6": r"\phi",      "\u03c7": r"\chi",
    "\u03c8": r"\psi",     "\u03c9": r"\omega",
    # Greek uppercase
    "\u0393": r"\Gamma",   "\u0394": r"\Delta",    "\u0398": r"\Theta",
    "\u039b": r"\Lambda",  "\u039e": r"\Xi",       "\u03a0": r"\Pi",
    "\u03a3": r"\Sigma",   "\u03a6": r"\Phi",
    "\u03a8": r"\Psi",     "\u03a9": r"\Omega",
    # Math operators & relations
    "\u2212": "-",
    "\u00b1": r"\pm",      "\u00d7": r"\times",    "\u00f7": r"\div",
    "\u00b7": r"\cdot",    "\u2219": r"\cdot",
    "\u221e": r"\infty",   "\u2211": r"\sum",      "\u220f": r"\prod",
    "\u222b": r"\int",     "\u2202": r"\partial",
    "\u2264": r"\leq",     "\u2265": r"\geq",
    "\u2260": r"\neq",     "\u2248": r"\approx",   "\u2261": r"\equiv",
    "\u221d": r"\propto",
    # Arrows
    "\u2190": r"\leftarrow",    "\u2192": r"\rightarrow",
    "\u2191": r"\uparrow",      "\u2193": r"\downarrow",
    "\u2194": r"\leftrightarrow",
    "\u21d0": r"\Leftarrow",    "\u21d2": r"\Rightarrow",
    "\u21d4": r"\Leftrightarrow",
    "\u21d1": r"\Uparrow",      "\u21d3": r"\Downarrow",
    # Set theory & logic
    "\u2208": r"\in",      "\u2209": r"\notin",
    "\u2282": r"\subset",  "\u2283": r"\supset",
    "\u2286": r"\subseteq","\u2287": r"\supseteq",
    "\u222a": r"\cup",     "\u2229": r"\cap",
    "\u2205": r"\emptyset",
    "\u2200": r"\forall",  "\u2203": r"\exists",
    "\u2227": r"\wedge",   "\u2228": r"\vee",
    "\u00ac": r"\neg",
    # Circled operators
    "\u2295": r"\oplus",   "\u2297": r"\otimes",   "\u2296": r"\ominus",
    "\u2299": r"\odot",
    # Misc math
    "\u221a": r"\sqrt{}",  "\u2207": r"\nabla",
    "\u00b0": r"^\circ",   "\u2032": r"^{\prime}",  "\u2033": r"^{\prime\prime}",
    "\u22a4": r"\top",     "\u22a5": r"\bot",
    "\u22c6": r"\star",    "\u2217": r"\ast",
    "\u03d5": r"\phi",     "\u03f5": r"\epsilon",
    "\u00b5": r"\mu",      "\u00af": r"\bar{}",
    "\u02dc": r"\tilde{}",  "\u02c6": r"\hat{}",
    "\u25cf": r"\bullet",  "\u25a0": r"\blacksquare", "\u25a1": r"\square",
    "\u226a": r"\ll",      "\u226b": r"\gg",
    # Tilde / sim variants
    "\u223c": r"\sim",     "\u223d": r"\backsim",
    "\uff5e": r"\sim",     "\u2243": r"\simeq",   "\u2245": r"\cong",
    # Dots
    "\u22ee": r"\vdots",   "\u22ef": r"\cdots",
    "\u22f1": r"\ddots",   "\u22c5": r"\cdot",
    # Vertical bars / norms
    "\u2223": r"\mid",     "\u2225": r"\|",
    # Subscript/superscript digits
    "\u2070": r"^{0}",     "\u00b9": r"^{1}",      "\u00b2": r"^{2}",
    "\u00b3": r"^{3}",     "\u2074": r"^{4}",      "\u2075": r"^{5}",
    "\u2076": r"^{6}",     "\u2077": r"^{7}",      "\u2078": r"^{8}",
    "\u2079": r"^{9}",     "\u207a": r"^{+}",      "\u207b": r"^{-}",
    "\u2080": r"_{0}",     "\u2081": r"_{1}",      "\u2082": r"_{2}",
    "\u2083": r"_{3}",     "\u2084": r"_{4}",      "\u2085": r"_{5}",
    "\u2086": r"_{6}",     "\u2087": r"_{7}",      "\u2088": r"_{8}",
    "\u2089": r"_{9}",
    # Fullwidth variants
    "\uff0d": "-",
    "\ufe63": "-",
}

# Text-mode commands — do NOT wrap in $...$
_TEXT_MATH = {
    "\u2026": r"\ldots{}",
    "\u00a9": r"\textcopyright{}",
    "\u00ae": r"\textregistered{}",
    "\u2122": r"\texttrademark{}",
}

# Bullet-like chars → $\bullet$ (math mode so latex_escape preserves them)
_BULLETS = {
    "\u2022",  # • standard bullet
    "\u2023",  # ‣ triangular bullet
    "\u2043",  # ⁃ hyphen bullet
    "\u25E6",  # ◦ white bullet
    "\u25CB",  # ○ white circle
    "\u25AA",  # ▪ small black square
    "\u25AB",  # ▫ small white square
    "\uF0B7",  # PUA bullet (MS Office fonts)
    "\uF0A7",  # PUA section bullet
    "\uF076",  # PUA bullet variant
}

_UNICODE_SPACES = {
    "\u00a0", "\u2002", "\u2003", "\u2004", "\u2005",
    "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",
    "\u202f", "\u205f",
}

_STRIP_CHARS = {
    "\u0008", "\u001b", "\u200b", "\uf8ee", "\uf8ef",
    "\uf8f0", "\uf8f9", "\uf8fa", "\uf8fb",
    "\ufeff",
    "\u200c", "\u200d",
    "\u0338",   # combining long solidus overlay
    "\ufffd",   # unicode replacement character
}

_SAFE_RANGES = (
    (0x00C0, 0x024F),   # Latin Extended-A & B
    (0x0020, 0x007E),   # Basic ASCII
)


# ══════════════════════════════════════════════════════════════════════════════
# Math fragment post-processing (runs AFTER _clean)
# ══════════════════════════════════════════════════════════════════════════════

def _merge_adjacent_math(text: str) -> str:
    """Merge adjacent inline math fragments: $a$$b$ -> $ab$."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\$([^$]+)\$\$([^$]+)\$", r"$\1\2$", text)
    return text


def _brace_safe(inner: str) -> bool:
    """Return True if curly braces are balanced and never go negative."""
    depth = 0
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _consolidate_math_lines(text: str) -> str:
    """
    If >60% of a line's non-space content is inside $...$,
    strip all $ and re-wrap as a single $...$ expression.
    Pure prose lines are never touched.
    """
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        math_chars = sum(len(m) for m in re.findall(r"\$([^$]+)\$", stripped))
        total_nonspace = sum(1 for c in stripped if c != " ")

        if total_nonspace > 0 and math_chars / total_nonspace > 0.60:
            inner = re.sub(r"\$([^$]+)\$", r"\1", stripped).strip()
            if _brace_safe(inner):
                result.append(f"${inner}$")
            else:
                result.append(line)
        else:
            result.append(line)

    return "\n".join(result)


def _fix_superscript_space(text: str) -> str:
    """
    Collapse pdfplumber's "sigma space 2" artefact into proper superscript.
    "$\\sigma$ 2" -> "$\\sigma^{2}$"
    Safety: skip if fragment already has ^.
    """
    def _replace_pos(m):
        inner, digit = m.group(1), m.group(2)
        if "^" in inner:
            return m.group(0)
        return f"${inner}^{{{digit}}}$"

    def _replace_neg(m):
        inner, digit = m.group(1), m.group(2)
        if "^" in inner:
            return m.group(0)
        return f"${inner}^{{-{digit}}}$"

    text = re.sub(r'\$([^$]+)\$ (\d)(?=[\s\W]|$)', _replace_pos, text)
    text = re.sub(r'\$([^$]+)\$ -(\d)(?=[\s\W]|$)', _replace_neg, text)
    return text


def _compact_equation_lines(text: str) -> str:
    """
    Remove pdfplumber's intra-formula spaces on equation-numbered lines.
    Only runs on lines ending with (N).
    """
    lines_out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if re.search(r"\(\d+\)\s*$", stripped):
            stripped = re.sub(r"(\w)\s+\(", r"\1(", stripped)
            stripped = re.sub(r"\(\s+", "(", stripped)
            stripped = re.sub(r"\s+\)", ")", stripped)
            lines_out.append(stripped)
        else:
            lines_out.append(line)
    return "\n".join(lines_out)


# Matches a standalone equation number like (1), (12), (123).
# Negative lookbehind: must NOT be preceded by letter, digit, or comma.
# Negative lookahead:  must NOT be followed by letter, digit, or comma.
# This distinguishes "(3)" (equation label) from "(f(X),σ²I)" or "(2019)".
_EQ_NUM_RE = re.compile(r"(?<![,a-zA-Z0-9])\((\d{1,3})\)(?![,a-zA-Z0-9])")


def _separate_numbered_equations(text: str) -> str:
    """
    Split collapsed equation runs into separate paragraphs.

    PDFs often extract numbered display equations as a single line/paragraph:
        'Prior: f() (0,k(,)) (1) ... Likelihood: ... (2) ... Posterior: ... (3)'

    This function detects 3+ consecutive equation numbers in a math-heavy
    paragraph and splits them, giving each equation its own paragraph.
    Equations are separated by double newlines so latex_paragraphs() renders
    a blank line between them.

    Safety guards:
    - Only triggers if 3+ equation-number matches are found.
    - Only triggers if the paragraph contains inline math ($...$), confirming
      it is a math-heavy block rather than prose referencing equations.
    - Years like (2019) are excluded by the 1-3 digit limit.
    - Expressions like (f(X),σ²I) are excluded by the letter/comma guards.
    """
    paragraphs = text.split("\n\n")
    result_paras = []

    for para in paragraphs:
        matches = list(_EQ_NUM_RE.finditer(para))

        # Require at least 3 equation numbers to avoid splitting prose refs
        if len(matches) < 3:
            result_paras.append(para)
            continue

        # Require inline math in the paragraph (confirms math-heavy context)
        if "$" not in para:
            result_paras.append(para)
            continue

        # Split: each piece runs from after the previous equation number up
        # to (and including) the current equation number.
        pieces = []
        prev_end = 0
        for m in matches:
            piece = para[prev_end:m.end()].strip()
            if piece:
                pieces.append(piece)
            prev_end = m.end()

        # Any trailing text after the last equation number (next prose paragraph)
        tail = para[prev_end:].strip()
        if tail:
            pieces.append(tail)

        if len(pieces) > 1:
            # Strip leading column-separator artifacts ($\cdot$, |, ·) from
            # pieces 2+ — these are two-column PDF layout artefacts that appear
            # between equations but have no semantic value.
            cleaned = [pieces[0]]
            _LEAD_JUNK = re.compile(
                r"^(?:\$\\cdot\$\s*|\$\\cdot\\cdot\$\s*|\|\s*|\$\\\|\$\s*)+",
            )
            for piece in pieces[1:]:
                cleaned.append(_LEAD_JUNK.sub("", piece).strip())
            result_paras.append("\n\n".join(p for p in cleaned if p))
        else:
            result_paras.append(para)

    return "\n\n".join(result_paras)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def normalize(doc: Document) -> Document:
    """Apply all text normalizations to a Document (returns a new copy)."""
    doc = deepcopy(doc)

    doc.title = _clean(doc.title)
    doc.keywords = [_clean(k) for k in doc.keywords]
    doc.authors = [
        Author(
            name=_clean(a.name),
            department=_clean(a.department),
            organization=_clean(a.organization),
            city=_clean(a.city),
            country=_clean(a.country),
            email=_clean(a.email),
        )
        for a in doc.authors
    ]
    doc.references = [
        Reference(index=r.index, text=_clean(r.text)) for r in doc.references
    ]

    # Abstract and section bodies get full math post-processing
    doc.abstract = _clean_with_math(doc.abstract)

    doc.sections = [
        Section(
            heading=_clean(s.heading),
            body=_clean_with_math(s.body),
            depth=s.depth,
            tables=[
                Table(
                    caption=_clean(t.caption),
                    headers=[_clean(h) for h in t.headers],
                    rows=[[_clean(c) for c in row] for row in t.rows],
                    notes=_clean(t.notes),
                )
                for t in s.tables
            ],
            figures=[
                Figure(
                    caption=_clean(f.caption) if f.caption else "",
                    image_path=f.image_path,
                    label=f.label,
                )
                for f in s.figures
            ],
        )
        for s in doc.sections
    ]

    return doc


def _clean_with_math(text: str) -> str:
    """
    Six-step pipeline for body/abstract:
      1. _clean()                          — unicode -> $\\cmd$ fragments
      2. _merge_adjacent_math()            — $a$$b$ -> $ab$
      3. _fix_superscript_space()          — "$\\sigma$ 2" -> "$\\sigma^{2}$"
      4. _consolidate_math_lines()         — heavy math lines -> single $...$
      5. _compact_equation_lines()         — remove pdfplumber spacing in eq lines
      6. _separate_numbered_equations()    — split collapsed eq runs into paragraphs
    """
    if not text:
        return ""
    text = _clean(text)
    text = _merge_adjacent_math(text)
    text = _fix_superscript_space(text)
    text = _consolidate_math_lines(text)
    text = _compact_equation_lines(text)
    text = _separate_numbered_equations(text)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Core character-level cleaner
# ══════════════════════════════════════════════════════════════════════════════

def _clean(text: str) -> str:
    """Character-level cleaning: ligatures, unicode spaces, math symbols."""
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

    # Text-mode commands (no $ wrapper)
    for sym, lat in _TEXT_MATH.items():
        text = text.replace(sym, lat)

    # Bullet chars → $\bullet$ (math mode survives latex_escape)
    for ch in _BULLETS:
        if ch in text:
            text = text.replace(ch, r"$\bullet$")

    # Math symbols — wrap each in $...$
    for sym, lat in _MATH.items():
        if sym in text:
            text = text.replace(sym, f"${lat}$")

    # De-hyphenation
    text = re.sub(r"(\w)\u00ad?\s*-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Safety net: remaining non-ASCII that pdflatex can't handle
    cleaned = []
    for ch in text:
        cp = ord(ch)
        if cp <= 0x7E:
            cleaned.append(ch)
        elif any(lo <= cp <= hi for lo, hi in _SAFE_RANGES):
            cleaned.append(ch)
        else:
            decomposed = unicodedata.normalize("NFKD", ch)
            ascii_approx = decomposed.encode("ascii", "ignore").decode("ascii")
            if not ascii_approx:
                log.debug("Safety net: unknown U+%04X in %r", ord(ch),
                          text[max(0, len(cleaned) - 15):len(cleaned) + 5])
            cleaned.append(ascii_approx if ascii_approx else "?")

    return "".join(cleaned).strip()
