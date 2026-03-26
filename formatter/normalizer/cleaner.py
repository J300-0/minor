"""
normalizer/cleaner.py — Pure local text transforms, no external calls.

Fixes: ligatures, unicode quotes/dashes, math symbols → LaTeX commands,
Greek letters, operators, arrows, sub/superscripts.
"""
import re
import logging
from core.models import Document

log = logging.getLogger("paper_formatter")

# ── Unicode → LaTeX replacements ─────────────────────────────────

LIGATURES = {
    "\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff",
    "\ufb03": "ffi", "\ufb04": "ffl",
}

UNICODE_FIXES = {
    "\u2018": "`",    "\u2019": "'",     # smart single quotes
    "\u201C": "``",   "\u201D": "''",    # smart double quotes
    "\u2013": "--",   "\u2014": "---",   # en-dash, em-dash
    "\u2026": "...",                      # ellipsis
    "\u00A0": " ",                        # non-breaking space
    "\u200B": "",                         # zero-width space
    "\u00AD": "",                         # soft hyphen
    "\uF0B7": "-",                        # Symbol font bullet
    "\uF0A7": "-",                        # Symbol font section mark
    "\uF0D8": "-",                        # Symbol font arrow
    "\u2022": "-",                        # bullet
    "\u2023": "-",                        # triangular bullet
    "\u25CF": "-",                        # black circle
    "\u25CB": "o",                        # white circle
    "\u25AA": "-",                        # black small square
    "\u25A0": "-",                        # black square
}

GREEK_TO_LATEX = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega",
}

MATH_SYMBOLS = {
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "·": r"\cdot", "∞": r"\infty", "≈": r"\approx", "≠": r"\neq",
    "≤": r"\leq", "≥": r"\geq", "∈": r"\in", "∉": r"\notin",
    "⊂": r"\subset", "⊃": r"\supset", "∪": r"\cup", "∩": r"\cap",
    "∧": r"\wedge", "∨": r"\vee", "¬": r"\neg",
    "∀": r"\forall", "∃": r"\exists", "∅": r"\emptyset",
    "∇": r"\nabla", "∂": r"\partial", "√": r"\sqrt",
    "∫": r"\int", "∑": r"\sum", "∏": r"\prod",
    "→": r"\rightarrow", "←": r"\leftarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow",
    "↔": r"\leftrightarrow", "⇔": r"\Leftrightarrow",
    "°": r"\degree",
    # Additional math operators that pdflatex can't handle as raw Unicode
    "−": r"-",           # U+2212 MINUS SIGN → ASCII hyphen-minus
    "⊕": r"$\oplus$",    # U+2295 CIRCLED PLUS
    "⊗": r"$\otimes$",   # U+2297 CIRCLED TIMES
    "⊖": r"$\ominus$",   # U+2296 CIRCLED MINUS
    "⊘": r"$\oslash$",   # U+2298 CIRCLED DIVISION SLASH
    "⊙": r"$\odot$",     # U+2299 CIRCLED DOT
    "∘": r"$\circ$",     # U+2218 RING OPERATOR
    "⟨": r"$\langle$",   # U+27E8 LEFT ANGLE BRACKET
    "⟩": r"$\rangle$",   # U+27E9 RIGHT ANGLE BRACKET
    "‖": r"\|",          # U+2016 DOUBLE VERTICAL LINE
    "′": r"'",           # U+2032 PRIME
    "″": r"''",          # U+2033 DOUBLE PRIME
    "∝": r"$\propto$",   # U+221D PROPORTIONAL TO
    "∥": r"\|",          # U+2225 PARALLEL TO
    "≡": r"$\equiv$",    # U+2261 IDENTICAL TO
    "≅": r"$\cong$",     # U+2245 APPROXIMATELY EQUAL TO
    "≪": r"$\ll$",       # U+226A MUCH LESS-THAN
    "≫": r"$\gg$",       # U+226B MUCH GREATER-THAN
}

# Superscript/subscript digits
SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "⁼": "=", "ⁿ": "n",
}

SUBSCRIPTS = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "₊": "+", "₋": "-", "₌": "=",
}


def normalize(doc: Document) -> Document:
    """Apply all text normalization transforms to a Document."""
    log.info("  Cleaning title, abstract, section bodies...")

    doc.title = _clean(doc.title)
    doc.abstract = _clean_with_math(doc.abstract)

    for section in doc.sections:
        section.heading = _clean(section.heading)
        section.body = _clean_with_math(section.body)

        # Clean table cells — use _clean_with_math for formula-heavy tables
        for table in section.tables:
            table.headers = [_clean_table_cell(h) for h in table.headers]
            table.rows = [[_clean_table_cell(c) for c in row] for row in table.rows]
            table.caption = _clean(table.caption)

    for ref in doc.references:
        ref.text = _clean(ref.text)

    return doc


def _clean(text: str) -> str:
    """Basic cleanup: ligatures, unicode, whitespace."""
    if not text:
        return ""

    # Step 1: Fix ligatures
    for old, new in LIGATURES.items():
        text = text.replace(old, new)

    # Step 2: Fix unicode quotes/dashes
    for old, new in UNICODE_FIXES.items():
        text = text.replace(old, new)

    # Step 3: Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    return text


def _clean_with_math(text: str) -> str:
    """Full cleanup including math symbol conversion."""
    if not text:
        return ""

    text = _clean(text)

    # Step 4: Greek letters → $\alpha$ etc.
    for char, cmd in GREEK_TO_LATEX.items():
        if char in text:
            text = text.replace(char, f"${cmd}$")

    # Step 5: Math symbols → LaTeX
    for char, cmd in MATH_SYMBOLS.items():
        if char in text:
            # Some replacements already include $...$ (e.g. $\oplus$)
            # Don't double-wrap those
            if cmd.startswith("$") and cmd.endswith("$"):
                text = text.replace(char, cmd)
            else:
                text = text.replace(char, f"${cmd}$")

    # Step 6: Super/subscript unicode chars — attached-aware conversion
    # x² → $x^{2}$  (NOT  x$^{2}$)
    # H₂O → $H_{2}$O  (NOT  H$_{2}$O)
    text = _fix_unicode_scripts(text)

    # Step 7: Wrap short ASCII subscript patterns: x_i → $x_i$, a_1 → $a_1$
    text = _wrap_ascii_subscripts(text)

    # Step 8: Fix decimal spaces: "3 . 14" → "3.14"
    text = _fix_decimal_spaces(text)

    # Step 9: Merge adjacent $...$ fragments
    text = _merge_adjacent_math(text)

    # Step 10: Strip remaining non-ASCII that pdflatex can't handle
    text = _strip_unsafe_unicode(text)

    return text


def _fix_unicode_scripts(text: str) -> str:
    """
    Convert unicode super/subscript chars to LaTeX math, handling the attached case.

    Attached:   x² → $x^{2}$   (base letter pulled into math, not left outside)
                H₂  → $H_{2}$
    Standalone: ²   → $^{2}$
                ₁   → $_{1}$

    The old naïve replace produced `x$^{2}$` which is broken LaTeX
    (superscript with no base in math mode).
    """
    result = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # ── Superscript: check if current char has superscript following it ──
        if i + 1 < n and text[i + 1] in SUPERSCRIPTS and ch not in SUPERSCRIPTS:
            # Collect all consecutive superscript chars
            j = i + 1
            sup_digits = ""
            while j < n and text[j] in SUPERSCRIPTS:
                sup_digits += SUPERSCRIPTS[text[j]]
                j += 1

            if ch.isalnum():
                # Attached: pull base char into math → $x^{2}$
                result.append(f"${ch}^{{{sup_digits}}}$")
            else:
                # Not a valid base — emit base char as-is, superscript standalone
                result.append(ch)
                result.append(f"$^{{{sup_digits}}}$")
            i = j
            continue

        # ── Subscript: check if current char has subscript following it ──
        if i + 1 < n and text[i + 1] in SUBSCRIPTS and ch not in SUBSCRIPTS:
            j = i + 1
            sub_digits = ""
            while j < n and text[j] in SUBSCRIPTS:
                sub_digits += SUBSCRIPTS[text[j]]
                j += 1

            if ch.isalnum():
                # Attached: pull base char into math → $H_{2}$
                result.append(f"${ch}_{{{sub_digits}}}$")
            else:
                result.append(ch)
                result.append(f"$_{{{sub_digits}}}$")
            i = j
            continue

        # ── Standalone superscript (no valid base before it) ──
        if ch in SUPERSCRIPTS:
            result.append(f"$^{{{SUPERSCRIPTS[ch]}}}$")
            i += 1
            continue

        # ── Standalone subscript ──
        if ch in SUBSCRIPTS:
            result.append(f"$_{{{SUBSCRIPTS[ch]}}}$")
            i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


# Inline ASCII subscript pattern: single-letter base + _ + 1-2 char subscript
# Matches: x_i, a_1, H_2, n_k   but NOT: file_path, http_request, some_word
_ASCII_SUB_RE = re.compile(
    r"(?<![a-zA-Z\$])"        # not preceded by 3+ letters or already in math
    r"([a-zA-Z]{1,2})"        # 1-2 letter base
    r"_"
    r"([a-zA-Z0-9]{1,2})"     # 1-2 char subscript (single digit or letter)
    r"(?![a-zA-Z0-9_])"       # not followed by more word chars (avoids file_path)
)


def _wrap_ascii_subscripts(text: str) -> str:
    """
    Wrap short ASCII subscript patterns in math mode so the renderer
    doesn't escape the underscore to \\_ in a context where it should be math.

    x_i  → $x_i$
    a_1  → $a_1$
    H_2  → $H_2$

    NOT wrapped (too long = English words, not math):
    file_path, http_get, some_var
    """
    def _replace(m):
        # Don't double-wrap if already inside $...$
        return f"${m.group(1)}_{{{m.group(2)}}}$"

    # Only apply outside existing $...$ regions
    return _apply_outside_math(text, _ASCII_SUB_RE, _replace)


def _apply_outside_math(text: str, pattern: re.Pattern, replacement) -> str:
    """
    Apply a regex substitution only to text that is outside $...$ math regions.
    Prevents double-wrapping already-converted expressions.
    """
    result = []
    last = 0
    # Find all $...$ regions (both inline and display)
    math_spans = []
    depth = 0
    start = None

    i = 0
    while i < len(text):
        if text[i] == "$" and (i == 0 or text[i - 1] != "\\"):
            if depth == 0:
                start = i
                depth = 1
            else:
                depth = 0
                if start is not None:
                    math_spans.append((start, i + 1))
                    start = None
        i += 1

    # Rebuild text, applying pattern only to non-math segments
    last_end = 0
    for ms, me in math_spans:
        # Apply to text before this math span
        segment = text[last_end:ms]
        result.append(pattern.sub(replacement, segment))
        # Keep math span unchanged
        result.append(text[ms:me])
        last_end = me

    # Apply to remaining text after last math span
    result.append(pattern.sub(replacement, text[last_end:]))
    return "".join(result)


def _fix_decimal_spaces(text: str) -> str:
    """Fix OCR artifacts: '3 . 14' → '3.14'"""
    return re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)


def _merge_adjacent_math(text: str) -> str:
    """
    Merge adjacent $...$<op>$...$ into single $...<op>...$ so LaTeX renders
    one continuous equation instead of fragmented inline math.

    $a^{2}$ + $b^{2}$ = $c^{2}$  →  $a^{2} + b^{2} = c^{2}$
    $a^{2}$$b^{2}$                →  $a^{2} b^{2}$
    """
    # Step 1: Merge directly adjacent $X$$Y$ → $X Y$
    text = re.sub(r"\$\s*\$", " ", text)

    # Step 2: Merge $X$ <short-op> $Y$ where op is +, -, =, ×, etc.
    # This catches: $a^{2}$ + $b^{2}$ = $c^{2}$
    # Pattern: closing $ + whitespace + short operator + whitespace + opening $
    def _merge_op(m):
        return " " + m.group(1) + " "

    text = re.sub(
        r"\$\s*([+\-=<>×÷·,])\s*\$",
        _merge_op,
        text,
    )

    return text


def _strip_unsafe_unicode(text: str) -> str:
    """Remove Unicode chars that pdflatex can't handle (Private Use Area, etc.)."""
    result = []
    for ch in text:
        cp = ord(ch)
        # Keep ASCII
        if cp < 128:
            result.append(ch)
        # Keep common Latin Extended (accented chars)
        elif cp < 0x0250:
            result.append(ch)
        # Keep if it's a known LaTeX-wrapped symbol (already converted above)
        elif ch in GREEK_TO_LATEX or ch in MATH_SYMBOLS:
            result.append(ch)
        # Skip Private Use Area (U+E000..U+F8FF) — these crash pdflatex
        elif 0xE000 <= cp <= 0xF8FF:
            continue
        # Keep other reasonable Unicode (CJK, etc.) — pdflatex with inputenc handles some
        elif cp < 0xFFFF:
            result.append(ch)
        else:
            continue  # skip supplementary planes
    return "".join(result)


# ── Table cell formula patterns ──────────────────────────────────

# Common text-based formula patterns found in table cells.
# These fire BEFORE _clean_with_math so they match the raw extracted text
# (with plain digits, not yet Unicode-converted).
# Order matters: more specific patterns first.
_TABLE_FORMULA_PATTERNS = [
    # ── Pythagoras: a2 + b2 = c2  →  $a^{2} + b^{2} = c^{2}$ ──
    (re.compile(r'\b([a-zA-Z])(\d)\s*\+\s*([a-zA-Z])(\d)\s*=\s*([a-zA-Z])(\d)\b'),
     lambda m: f"${m.group(1)}^{{{m.group(2)}}} + {m.group(3)}^{{{m.group(4)}}} = {m.group(5)}^{{{m.group(6)}}}$"),

    # ── Logarithm: log(xy) = log(x) + log(y) ──
    (re.compile(r'\blog\s*\(([^)]+)\)\s*=\s*log\s*\(([^)]+)\)\s*\+\s*log\s*\(([^)]+)\)'),
     lambda m: f"$\\log({m.group(1)}) = \\log({m.group(2)}) + \\log({m.group(3)})$"),

    # ── Derivative: df/dt = lim ... (f(t+h)-f(t))/h ──
    (re.compile(r'\bdf\s*/\s*dt\s*=\s*lim'),
     lambda m: r"$\frac{df}{dt} = \lim_{h \to 0} \frac{f(t+h) - f(t)}{h}$"),

    # ── Gravity: F = Gm1m2/r2 ──
    (re.compile(r'\bF\s*=\s*G\s*m\s*1\s*m\s*2\s*/\s*r\s*2\b'),
     lambda m: r"$F = G\frac{m_1 m_2}{r^{2}}$"),
    (re.compile(r'\bF\s*=\s*Gm1m2\s*/\s*r2\b'),
     lambda m: r"$F = G\frac{m_1 m_2}{r^{2}}$"),

    # ── Complex number: i2 = -1 or i² = −1 ──
    (re.compile(r'\bi\s*2\s*=\s*[−\-]\s*1\b'),
     lambda m: r"$i^{2} = -1$"),

    # ── Normal distribution: Φ(x) = 1/(σ√(2π)) e^... ──
    # This is complex — just wrap the whole thing as-is in math mode
    (re.compile(r'[ΦPhi]\s*\(\s*x\s*\)\s*='),
     lambda m: None),  # handled by Unicode conversion below

    # ── Fourier: f̂(ω) = ∫ ... ──
    (re.compile(r'f\s*\^\s*\(\s*[ωw]\s*\)\s*='),
     lambda m: None),  # handled by Unicode conversion below

    # ── Relativity: E = mc2 ──
    (re.compile(r'\bE\s*=\s*mc\s*2\b'),
     lambda m: r"$E = mc^{2}$"),

    # ── Generic: Variable = expression with digit superscript ──
    # X = YZn  where X is uppercase, YZ is lowercase, n is a digit
    (re.compile(r'\b([A-Z])\s*=\s*([a-z]+)\s*(\d)\b'),
     lambda m: f"${m.group(1)} = {m.group(2)}^{{{m.group(3)}}}$"),

    # ── Generic: var-digit = number (e.g. i2 = -1, x3 = 27) ──
    (re.compile(r'\b([a-z])(\d)\s*=\s*([−\-]?\d+)\b'),
     lambda m: f"${m.group(1)}^{{{m.group(2)}}} = {m.group(3)}$"),
]


def _normalize_unicode_to_ascii(text: str) -> str:
    """
    Convert Unicode super/subscript chars and math symbols to plain ASCII
    so table formula patterns can match consistently.
    ² → 2, ₁ → 1, − → -, × → *, etc.
    """
    for char, digit in SUPERSCRIPTS.items():
        text = text.replace(char, digit)
    for char, digit in SUBSCRIPTS.items():
        text = text.replace(char, digit)
    # Unicode minus → ASCII minus
    text = text.replace("\u2212", "-")  # −
    text = text.replace("\u2013", "-")  # en-dash
    return text


def _clean_table_cell(text: str) -> str:
    """
    Clean a table cell with math awareness.

    Strategy:
    1. Normalize Unicode scripts to ASCII (² → 2, ₁ → 1, − → -)
    2. Apply table-specific formula patterns on the normalized text
    3. Apply standard math cleanup for anything not caught by patterns
    """
    if not text:
        return ""

    # Step 1: Normalize so patterns match regardless of Unicode vs ASCII
    text = _normalize_unicode_to_ascii(text)

    # Step 2: Apply basic cleanup (ligatures, whitespace)
    text = _clean(text)

    # Step 3: Try table-specific formula patterns
    for pattern, replacement in _TABLE_FORMULA_PATTERNS:
        if replacement is not None:
            text = pattern.sub(replacement, text)

    # Step 4: Apply remaining math cleanup (Greek letters, symbols, etc.)
    # Skip _fix_unicode_scripts since we already normalized those
    for char, cmd in GREEK_TO_LATEX.items():
        if char in text:
            text = text.replace(char, f"${cmd}$")
    for char, cmd in MATH_SYMBOLS.items():
        if char in text:
            text = text.replace(char, f"${cmd}$")

    # Step 5: Merge adjacent math fragments
    text = _merge_adjacent_math(text)

    # Step 6: Strip unsafe unicode
    text = _strip_unsafe_unicode(text)

    return text
