"""
normalizer/cleaner.py — Pure local text transforms, no external calls.

Fixes: ligatures, unicode quotes/dashes, math symbols → LaTeX commands,
Greek letters, operators, arrows, sub/superscripts.
"""
import re
import logging
from core.models import Document
from core.shared import MATH_CHARS as SHARED_MATH_CHARS, count_real_words

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
    "∇": r"\nabla", "∂": r"\partial", "√": r"\sqrt{}",
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
    # Table annotation symbols (common in results tables)
    # NOTE: values containing $...$ are already math-wrapped — the code checks for this
    # and skips re-wrapping (see _clean_with_math and _clean_table_cell)
    "⇑": r"$\Uparrow$",  # U+21D1 significant improvement
    "⇓": r"$\Downarrow$", # U+21D3 significant decline
    "↑": r"$\uparrow$",  # U+2191 improvement
    "↓": r"$\downarrow$", # U+2193 decline
    "⋆": r"$\star$",     # U+22C6 STAR OPERATOR
    "□": r"$\square$",   # U+25A1 WHITE SQUARE (QED marker)
    "†": r"$\dagger$",   # U+2020 DAGGER
    "‡": r"$\ddagger$",  # U+2021 DOUBLE DAGGER
    "§": r"\S{}",         # U+00A7 SECTION SIGN
    "¶": r"\P{}",         # U+00B6 PILCROW SIGN
    "★": r"$\bigstar$",  # U+2605 BLACK STAR
    "✓": r"$\checkmark$", # U+2713 CHECK MARK
    "✗": r"$\times$",    # U+2717 BALLOT X
    "∼": r"$\sim$",      # U+223C TILDE OPERATOR
    "⊤": r"$\top$",      # U+22A4 DOWN TACK (transpose)
    "⊥": r"$\bot$",      # U+22A5 UP TACK (perpendicular)
    "⊢": r"$\vdash$",    # U+22A2 RIGHT TACK
    "⊣": r"$\dashv$",    # U+22A3 LEFT TACK
    "∗": r"$*$",          # U+2217 ASTERISK OPERATOR
    "ϕ": r"$\phi$",      # U+03D5 PHI SYMBOL (variant)
    "ϵ": r"$\epsilon$",  # U+03F5 LUNATE EPSILON SYMBOL (variant)
    "ϑ": r"$\vartheta$", # U+03D1 THETA SYMBOL (variant)
    "ϱ": r"$\varrho$",   # U+03F1 RHO SYMBOL (variant)
    "ϖ": r"$\varpi$",    # U+03D6 PI SYMBOL (variant)
    "ℓ": r"$\ell$",      # U+2113 SCRIPT SMALL L
    "ℝ": r"$\mathbb{R}$", # U+211D DOUBLE-STRUCK R
    "ℕ": r"$\mathbb{N}$", # U+2115 DOUBLE-STRUCK N
    "ℤ": r"$\mathbb{Z}$", # U+2124 DOUBLE-STRUCK Z
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


def _merge_orphan_symbol_lines(text: str) -> str:
    """
    Merge orphaned math symbol lines back into surrounding text.

    PDF extractors (especially pdfplumber) often split inline math symbols
    (Greek letters, subscripts, operators) onto separate lines because they're
    rendered in a different font position. This produces:

        there is a server holding weights   and global hyperparam-
        θ
        eters   that denote the noise level
        σ, λ

    This function detects short lines containing only math symbols/characters
    and merges them into the adjacent text lines where they belong.
    """
    if not text:
        return text

    # Characters that commonly appear as orphaned math symbols
    MATH_CHARS = SHARED_MATH_CHARS

    lines = text.split("\n")
    if len(lines) <= 1:
        return text

    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check if this is an orphaned symbol line:
        # - Short (≤ 15 chars)
        # - Contains mostly math chars, single letters, commas, parens, spaces
        # - NOT a regular heading or sentence
        is_orphan = False
        if stripped and len(stripped) <= 15:
            non_space = stripped.replace(" ", "").replace(",", "").replace("(", "").replace(")", "")
            if non_space and all(
                c in MATH_CHARS
                or (c.isalpha() and len(non_space) <= 6)
                or c.isdigit()  # subscript/superscript indices: σ(1)π(1), n4, etc.
                or c in ".,;:=+-*/^_{}|[]<>"
                for c in non_space
            ):
                # Additional check: orphan lines are usually 1-5 distinct chars
                # and don't look like regular words
                words = stripped.split()
                if all(len(w) <= 3 or any(c in MATH_CHARS for c in w) for w in words):
                    # Don't merge if it looks like a real word
                    real_words = count_real_words(words)
                    if not real_words:
                        # Digit-only lines (like "4", "22") must contain math
                        # context or be adjacent to math — skip pure page numbers
                        if non_space.isdigit() and len(non_space) > 3:
                            pass  # likely page number, not a subscript
                        else:
                            is_orphan = True

        if is_orphan and result:
            prev = result[-1]
            prev_stripped = prev.rstrip()

            # For digit-only orphans, decide merge direction:
            # If previous line has a dangling variable (e.g., "O(n )" or "A"),
            # it's likely a super/subscript → merge up. Otherwise try merging down
            # to see if it belongs with the next line.
            if stripped.replace(" ", "").isdigit() and i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                # Check if prev ends with a single-letter variable or "letter )"
                # suggesting the digit is a subscript/superscript
                # e.g., "matrix A" → "A22", "O(n )" → "O(n⁴)"
                # Must be a SINGLE letter (not end of a word like "we")
                prev_wants_digit = bool(re.search(
                    r"(?:^|\s|\()([a-zA-Z])\s*$"        # trailing single letter
                    r"|[a-zA-Z]\s*\)\s*$",               # letter followed by )
                    prev_stripped
                ))
                if not prev_wants_digit and next_stripped:
                    # Merge downward instead (prepend with space)
                    lines[i + 1] = stripped + " " + lines[i + 1].lstrip()
                    i += 1
                    continue

            # Merge into the previous line
            if prev_stripped.endswith("-"):
                # Hyphenated line break — symbol goes after dehyphenation
                result[-1] = prev_stripped[:-1] + stripped
            elif "  " in prev_stripped:
                # There's a gap in the previous line — insert the symbol there
                # Replace the LAST double-space gap with the symbol
                last_gap = prev_stripped.rfind("  ")
                result[-1] = (prev_stripped[:last_gap] + " " + stripped
                              + " " + prev_stripped[last_gap:].lstrip())
            else:
                # Just append to previous line with a space
                result[-1] = prev_stripped + " " + stripped
        elif is_orphan and not result and i + 1 < len(lines):
            # First line is an orphan — prepend to next line
            next_line = lines[i + 1]
            lines[i + 1] = stripped + " " + next_line.lstrip()
        else:
            result.append(line)

        i += 1

    return "\n".join(result)


def _remove_garbled_math_blocks(text: str) -> str:
    """
    Remove multi-line garbled equation blocks from body text.

    Complex equations (matrices, multi-line aligned math, summations with underbrace)
    get extracted by PyMuPDF as plain text that looks like:

        fk(X) - fk(X) =
        k(x1, x1) · · · k(x1, xn - 1) k(x1, xn) k(x2, x1) · · · k(x2, xn
        - 1) k(x2, xn)
        k(xn - 1, x1) · · · k(xn - 1, xn - 1) k(xn - 1, xn) k(xn, x1)

    Or:
        = ||[ 0, · · · , 0
        (n - 1)2 terms
        , m, · · · , m
        2n - 1 terms
        ]||2 2n - 1m

    These are impossible to reconstruct from text. The OCR pipeline captures them
    as images (formula_blocks). This function detects and removes the garbled text
    version so only the OCR'd image version appears in the output.

    Detection: groups of 3+ consecutive lines where most lines are "equation-like":
    - Short/medium length (not full paragraphs)
    - Heavy on math symbols, operators, parentheses
    - Very few real English words
    - Contain patterns: · · · (cdots), ||...|| (norms), repeated function calls
    """
    if not text:
        return text

    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        # Try to detect start of a garbled math block
        if _is_garbled_math_line(stripped):
            # Collect consecutive garbled math lines (allow 1 blank line gap)
            block_start = i
            block_lines = []
            blanks_in_row = 0

            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    blanks_in_row += 1
                    if blanks_in_row > 1:
                        break  # 2+ blank lines = end of block
                    block_lines.append(s)
                    i += 1
                    continue
                blanks_in_row = 0
                if _is_garbled_math_line(s):
                    block_lines.append(s)
                    i += 1
                else:
                    break

            # Count non-empty lines
            non_empty = [l for l in block_lines if l.strip()]

            if len(non_empty) >= 3:
                # This is a garbled math block — skip it
                log.debug("  Removed garbled math block (%d lines): %s...",
                          len(non_empty), non_empty[0][:60] if non_empty else "")
                continue
            else:
                # Not enough lines — keep them
                result.extend(lines[block_start:i])
                continue
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _is_math_garbage(line: str, mode: str = "block") -> bool:
    """
    Unified detector for garbled equation text.

    mode="block": detects garbled multi-line equation blocks (from _remove_garbled_math_blocks)
        - More permissive length (150 chars), needs stronger signals
    mode="fragment": detects single-line equation fragments (from _remove_fragmented_equations)
        - Stricter length (60 chars), catches isolated symbols/operators

    Returns True for lines that are equation fragments extracted as text,
    which cannot be meaningfully rendered and should be replaced by OCR images.
    """
    if not line:
        return mode == "fragment"  # empty lines are fragments, not blocks

    max_len = 150 if mode == "block" else 60
    if len(line) > max_len:
        return False

    # ── Definite indicators (block mode) ──────────────────────
    if mode == "block":
        # · · · (cdots pattern)
        if "· · ·" in line or ("..." in line and ("k(" in line or "||" in line)):
            return True
        # Repeated function-call patterns: k(x1, x1) k(x2, x1) etc.
        func_calls = re.findall(r'[a-zA-Z]\([^)]{1,20}\)', line)
        if len(func_calls) >= 3:
            return True
        # Norm notation fragments: ||..|| or ]||2
        if re.search(r'\|\|.*\|\|', line):
            return True
        # Lines that are just "N terms" (underbrace labels)
        if re.match(r'^[\d\w\s\-\+\(\)]*\bterms\b\s*$', line, re.IGNORECASE):
            return True
        # Lines starting with = (continuation of multi-line equation)
        if line.startswith("=") and len(line) < 100:
            words = line.split()
            real_words = count_real_words(words)
            if len(real_words) <= 1:
                return True

    # ── Character composition analysis (both modes) ───────────
    chars = line.replace(" ", "")
    if not chars:
        return mode == "fragment"

    math_count = sum(1 for c in chars if c in SHARED_MATH_CHARS)
    letter_count = sum(1 for c in chars if c.isalpha() and c.isascii())
    digit_count = sum(1 for c in chars if c.isdigit())
    operator_count = sum(1 for c in chars if c in "=+-*/(){}[]|,;:.<>^_\\¯")
    total = len(chars)

    # Fragment-specific checks
    if mode == "fragment":
        # Equation number like "(16)" or "(17)"
        if re.match(r"^\(\d+\)$", line):
            return True
        # Pure math symbols
        if math_count > 0 and math_count + operator_count >= total * 0.5 and total <= 20:
            return True
        # Mostly single letters and operators (like "c c c" or "= log ( ( ); )")
        words = line.split()
        single_chars = sum(1 for w in words if len(w) <= 2)
        if len(words) >= 2 and single_chars >= len(words) * 0.6 and total <= 40:
            real_words = count_real_words(words)
            if not real_words:
                return True
        # Line is just operators and parens
        if operator_count >= total * 0.6 and total <= 25:
            return True

    # Block-specific: general short-line math analysis
    if mode == "block" and len(line) < 100:
        words = line.split()
        real_words = count_real_words(words)
        if len(real_words) >= 3:
            return False

        math_op_count = math_count + operator_count
        ratio = (math_op_count + digit_count) / max(total, 1)
        if ratio > 0.35 and len(real_words) <= 1 and len(line) < 80:
            return True

    return False


def _is_garbled_math_line(line: str) -> bool:
    """Backwards-compatible wrapper."""
    return _is_math_garbage(line, mode="block")


def _remove_fragmented_equations(text: str) -> str:
    """
    Remove fragmented equation blocks from body text.

    PDF extractors split equations into multi-line fragments of isolated
    symbols that render as garbage. E.g.:
        ML
        ϕ σ , λ
        = log ( ( ); )
        Pr y X
        θ c c c

    These blocks are detected as sequences of 3+ short lines where most lines
    contain only symbols, single letters, operators, and parentheses.
    """
    if not text:
        return text

    MATH_CHARS = SHARED_MATH_CHARS

    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check if this line starts a fragmented equation block
        if _is_equation_fragment(stripped, MATH_CHARS):
            # Count consecutive equation fragment lines
            frag_start = i
            frag_count = 0
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    # Empty lines within a fragment block are OK
                    frag_count += 1
                    i += 1
                    continue
                if _is_equation_fragment(s, MATH_CHARS):
                    frag_count += 1
                    i += 1
                else:
                    break

            # Only remove if 3+ consecutive fragment lines
            if frag_count >= 3:
                # Skip the entire block (don't add to result)
                continue
            else:
                # Not enough fragments — keep the lines
                result.extend(lines[frag_start:i])
                continue
        else:
            result.append(line)
            i += 1

    return "\n".join(result)


def _is_equation_fragment(line: str, math_chars: set) -> bool:
    """Check if a line looks like a fragment from a broken equation."""
    return _is_math_garbage(line, mode="fragment")


def _remove_charperline_garbage(text: str) -> str:
    """
    Remove character-per-line garbled text from section bodies.

    Some PDF elements (rotated text, vertical labels) extract as one char per line:
        R
        M
        S
        E
    These produce garbage in the output. Detect sequences of 4+ consecutive very short
    lines (<=3 chars each) and remove them.
    """
    if not text:
        return ""

    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        # Check if this starts a run of very short lines
        run_start = i
        while i < len(lines) and len(lines[i].strip()) <= 3:
            i += 1
        run_len = i - run_start

        if run_len >= 4:
            # This is a char-per-line garbage run — skip it entirely
            pass
        else:
            # Keep these lines
            for j in range(run_start, i):
                result.append(lines[j])

        if i == run_start:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _remove_repeated_table_captions(text: str, table_captions: list) -> str:
    """
    Remove table caption text that leaked into body text.

    Table captions like 'Real world regression datasets, RMSE reported...'
    sometimes appear in section bodies because pdfplumber packs captions
    into the same text block as body paragraphs.
    """
    if not text or not table_captions:
        return text

    for caption in table_captions:
        if not caption or len(caption) < 20:
            continue
        # Use the first 40 chars of caption as search pattern
        search = caption[:min(40, len(caption))].strip()
        if search in text:
            # Remove lines containing this caption fragment
            lines = text.split("\n")
            lines = [l for l in lines if search not in l]
            text = "\n".join(lines)

    return text


def _remove_running_headers(text: str, title: str) -> str:
    """
    Remove running headers that match the document title.

    Many journals repeat the paper title (or a shortened version) as a running
    header on every page. PyMuPDF extracts these into the body text, producing
    orphan lines like:

        The Formulas
                                                                        3.
        ISSN 1339-9853 (online)
        http://acta-avionica.tuke.sk
        ISSN 1335-9479 (print)

    This function detects lines that match the title (case-insensitive) and
    removes them along with adjacent page numbers, ISSN lines, and URLs that
    are part of the running header block.
    """
    if not text or not title:
        return text

    # Normalize title for comparison
    title_clean = re.sub(r"\s+", " ", title).strip().lower()
    if len(title_clean) < 3:
        return text

    lines = text.split("\n")
    result = []
    i = 0
    removed = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        stripped_lower = re.sub(r"\s+", " ", stripped).strip().lower()

        # Check if this line matches the title (exact or contained)
        is_title_header = False
        if stripped_lower and (
            stripped_lower == title_clean
            or (len(title_clean) >= 5 and title_clean in stripped_lower
                and len(stripped_lower) < len(title_clean) + 10)
        ):
            is_title_header = True

        if is_title_header:
            removed += 1
            log.debug("  Removing running header line: %r", stripped)
            i += 1
            # Also skip adjacent metadata lines (page numbers, ISSN, URLs)
            while i < len(lines):
                next_stripped = lines[i].strip()
                if not next_stripped:
                    i += 1  # skip blank lines in header block
                    continue
                # Page number (just digits, possibly with dots/spaces)
                if re.match(r"^\d{1,3}\.\s*$", next_stripped):
                    log.debug("  Removing running header page number: %r", next_stripped)
                    i += 1
                    continue
                # ISSN line
                if re.match(r"ISSN\s*[\d-]+", next_stripped, re.I):
                    log.debug("  Removing running header ISSN: %r", next_stripped)
                    i += 1
                    continue
                # URL line
                if re.match(r"https?://", next_stripped, re.I):
                    log.debug("  Removing running header URL: %r", next_stripped)
                    i += 1
                    continue
                break  # not part of running header anymore
            continue

        result.append(line)
        i += 1

    if removed > 0:
        log.info("  Removed %d running header block(s) matching title %r", removed, title_clean)

    return "\n".join(result)


def normalize(doc: Document) -> Document:
    """Apply all text normalization transforms to a Document."""
    log.info("  Cleaning title, abstract, section bodies...")

    # Collect table captions for dedup
    table_captions = []
    for section in doc.sections:
        for table in section.tables:
            if table.caption:
                table_captions.append(table.caption)

    doc.title = _clean(doc.title)
    doc.abstract = _merge_orphan_symbol_lines(doc.abstract)
    doc.abstract = _clean_with_math(doc.abstract)

    for section in doc.sections:
        section.heading = _clean(section.heading)
        section.body = _remove_running_headers(section.body, doc.title)
        section.body = _remove_charperline_garbage(section.body)
        section.body = _remove_repeated_table_captions(section.body, table_captions)
        section.body = _merge_orphan_symbol_lines(section.body)
        section.body = _remove_garbled_math_blocks(section.body)
        section.body = _remove_fragmented_equations(section.body)
        section.body = _clean_with_math(section.body)

        # Clean table cells — use _clean_with_math for formula-heavy tables
        for table in section.tables:
            table.headers = [_clean_table_cell(h) for h in table.headers]
            table.rows = [[_clean_table_cell(c) for c in row] for row in table.rows]
            table.caption = _clean(table.caption)

    for ref in doc.references:
        ref.text = _clean_reference(ref.text)

    return doc


def _clean_reference(text: str) -> str:
    """Clean a reference entry for IEEE-style formatting."""
    if not text:
        return ""

    text = _clean(text)

    # Remove trailing periods that got doubled
    text = re.sub(r"\.\.+$", ".", text)

    # Normalize interpuncts used as author separators: · → ,
    text = text.replace(" · ", ", ")
    text = text.replace("·", ", ")

    # Remove superscript digits attached to author names (affiliation markers)
    text = re.sub(r"(\w)([¹²³⁴⁵⁶⁷⁸⁹⁰]+)", r"\1", text)
    for sup_char in SUPERSCRIPTS:
        text = text.replace(sup_char, "")

    # Clean up multiple spaces
    text = re.sub(r"\s+", " ", text).strip()

    return text


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

    # Step 3: Strip (cid:XX) CID-encoded character artifacts from PDF extraction
    text = re.sub(r"\(cid:\d+\)", "", text)

    # Step 4: Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    return text


def _clean_with_math(text: str) -> str:
    """Full cleanup including math symbol conversion."""
    if not text:
        return ""

    text = _clean(text)

    # Step 3b: Convert numbered equation lines into display math BEFORE
    # individual char replacements (so the whole line becomes one math block)
    text = _convert_numbered_equations(text)

    # Step 3c: Convert Greek-subscript patterns BEFORE individual Greek
    # char replacements (so ασ(1)π(1) becomes $\alpha_{\sigma(1)\pi(1)}$
    # before σ/π/α get individually wrapped in $...$)
    text = _fix_greek_subscript_patterns(text)

    # Step 4: Greek letters → $\alpha$ etc.
    # First, convert Greek chars INSIDE existing $...$ to bare LaTeX commands
    # (no extra $ wrapping since they're already in math mode)
    def _convert_greek_in_math(m):
        inner = m.group(1)
        for ch, cmd in GREEK_TO_LATEX.items():
            inner = inner.replace(ch, cmd)
        return f"${inner}$"
    text = re.sub(r"\$([^$]+)\$", _convert_greek_in_math, text)

    # Then convert remaining Greek chars (outside math mode) with $ wrapping
    for char, cmd in GREEK_TO_LATEX.items():
        if char in text:
            text = text.replace(char, f"${cmd}$")

    # Step 5: Math symbols → LaTeX
    # First convert math symbols inside existing $...$ (no extra $ wrapping)
    def _convert_symbols_in_math(m):
        inner = m.group(1)
        for ch, cmd in MATH_SYMBOLS.items():
            if ch in inner:
                bare = cmd[1:-1] if cmd.startswith("$") and cmd.endswith("$") else cmd
                inner = inner.replace(ch, bare)
        return f"${inner}$"
    text = re.sub(r"\$([^$]+)\$", _convert_symbols_in_math, text)

    # Then convert remaining math symbols (outside math mode)
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

    # Step 7b: Equation-context implicit subscripts: aij → $a_{ij}$ when near math operators
    text = _fix_implicit_subscripts(text)

    # Step 8: Fix decimal spaces: "3 . 14" → "3.14"
    text = _fix_decimal_spaces(text)

    # Step 9: Wrap standalone math-heavy lines in $...$
    text = _wrap_math_lines(text)

    # Step 10: Merge adjacent $...$ fragments
    text = _merge_adjacent_math(text)

    # Step 11: Fix double-superscript from stray ' inside math mode
    text = _fix_math_primes(text)

    # Step 12: Strip remaining non-ASCII that pdflatex can't handle
    text = _strip_unsafe_unicode(text)

    return text


def _convert_numbered_equations(text: str) -> str:
    """
    Detect numbered equation lines and convert them to LaTeX display equations.

    Targets lines like:
        Prior: w ∼ N (0, λ²I)                         (7)
        Likelihood: (y|X,w) ∼ N(w⊤Φ, σ²I)             (8)
        ¯m(·) = k(·, X)(K + σ2I) - 1y,                (4)
        (y*|x*, X, y) ∼ N(¯m(x*), σ2 + k(x*, x*))    (6)

    These are display equations embedded as text. They:
    - End with (N) where N is 1-3 digits (equation number)
    - Contain math chars (Greek, ∼, ∈, subscripts, etc.) OR function notation
    - Are relatively short (not full paragraphs)

    Converts the math portion to a LaTeX equation block with proper Greek/symbol
    substitution done inside math mode (so we get \sigma not $\sigma$).
    """
    if not text:
        return text

    # Regex: line ending with (digit) equation number, possibly with trailing whitespace
    eq_num_re = re.compile(r"^(.*?)\s*\((\d{1,3})\)\s*$")

    # Math content indicators (raw Unicode, before conversion)
    _math_chars = set("αβγδεζηθικλμνξπρσςτυφχψωΓΔΘΛΞΠΣΦΨΩ")
    _math_chars.update("∼≈≠≤≥∈∉⊂⊃∪∩∧∨¬∀∃∅∇∂√∫∑∏±×÷→←↔⊤⊥⊕⊗")

    # Label words that precede the equation (not math themselves)
    _label_re = re.compile(
        r"^((?:Prior|Likelihood|Posterior|Prediction|where|and|with|"
        r"s\.t\.|subject\s+to)\s*:?\s*)",
        re.IGNORECASE,
    )

    lines = text.split("\n")
    result = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        m = eq_num_re.match(stripped)
        if not m:
            result.append(line)
            continue

        body = m.group(1).strip()
        eq_num = m.group(2)

        # Check if the body has math content
        math_count = sum(1 for ch in body if ch in _math_chars)
        has_parens = "(" in body and ")" in body
        has_eq = "=" in body or "∼" in body or "~" in body or "≤" in body or "≥" in body or "<" in body or ">" in body

        # Need at least some math indicators — must have BOTH:
        # (a) math characters or comparison operators
        # (b) equation-like structure (parens, equals, operators)
        if math_count < 1 and not has_eq:
            result.append(line)
            continue

        # Skip lines that look like prose with a reference number at end
        # e.g. "The Formulas (5)." or "as shown in equation (3)"
        # Real equations have few English words; prose has many
        words = body.split()
        real_words = count_real_words(words)
        # If most tokens are real English words, this is prose with a ref number
        if len(real_words) >= 2 and len(real_words) >= len(words) * 0.4:
            result.append(line)
            continue

        # Also skip if body is very short and looks like a caption/label
        # e.g. "The Formulas" or "See equation"
        if len(body) < 30 and math_count == 0 and not has_eq:
            result.append(line)
            continue

        # Separate label (e.g., "Prior:") from math content
        label = ""
        math_body = body
        lm = _label_re.match(body)
        if lm:
            label = lm.group(1).strip()
            math_body = body[lm.end():].strip()

        # Convert special math patterns BEFORE generic symbol replacement
        # ⊤ after a letter = transpose: w⊤ → w^{\top}
        math_body = re.sub(r"([a-zA-Z])⊤", r"\1^{\\top}", math_body)
        math_body = math_body.replace("⊤", r"^{\top}")  # standalone ⊤
        # ⊥ = perpendicular
        math_body = math_body.replace("⊥", r"\perp")

        # Convert Greek and math symbols INSIDE the equation (not as separate $...$)
        for char, cmd in GREEK_TO_LATEX.items():
            math_body = math_body.replace(char, cmd)
        for char, cmd in MATH_SYMBOLS.items():
            if char in ("⊤", "⊥"):
                continue  # already handled above
            if cmd.startswith("$") and cmd.endswith("$"):
                # Unwrap from $...$: we're already in math mode
                math_body = math_body.replace(char, cmd[1:-1])
            else:
                math_body = math_body.replace(char, cmd)

        # Convert superscript Unicode chars inside math — group consecutive ones
        # e.g. ⁻¹ → ^{-1} (NOT ^{-}^{1})
        i_s = 0
        math_chars_list = list(math_body)
        new_chars = []
        while i_s < len(math_chars_list):
            ch = math_chars_list[i_s]
            if ch in SUPERSCRIPTS:
                sup = SUPERSCRIPTS[ch]
                i_s += 1
                while i_s < len(math_chars_list) and math_chars_list[i_s] in SUPERSCRIPTS:
                    sup += SUPERSCRIPTS[math_chars_list[i_s]]
                    i_s += 1
                new_chars.append(f"^{{{sup}}}")
            elif ch in SUBSCRIPTS:
                sub = SUBSCRIPTS[ch]
                i_s += 1
                while i_s < len(math_chars_list) and math_chars_list[i_s] in SUBSCRIPTS:
                    sub += SUBSCRIPTS[math_chars_list[i_s]]
                    i_s += 1
                new_chars.append(f"_{{{sub}}}")
            else:
                new_chars.append(ch)
                i_s += 1
        math_body = "".join(new_chars)

        # Handle common text patterns in equations
        # ¯x or ¯m → \bar{x}, \bar{m}  (macron/overline)
        math_body = re.sub(r"¯\s*([a-zA-Z])", r"\\bar{\1}", math_body)
        # f(·) notation — · midpoint to \cdot
        math_body = math_body.replace("·", r"\cdot")
        # w̃ (w + combining tilde) → \tilde{w}
        math_body = re.sub(r"([a-zA-Z])\u0303", r"\\tilde{\1}", math_body)
        # Strip trailing "where", "and", etc. from math body (they're text, not math)
        trail_m = re.search(r",?\s*\b(where|and|with)\s*$", math_body, re.IGNORECASE)
        trail_text = ""
        if trail_m:
            trail_text = trail_m.group(0).strip().rstrip(",").strip()
            math_body = math_body[:trail_m.start()].strip()

        # Strip $...$ wrappers from math_body — we're going into equation* (math mode)
        # The extractor may have wrapped subscripts as $t_{i}$ which would break inside equation*
        math_body = re.sub(r"\$([^$]+)\$", r"\1", math_body)

        # Build the output as a proper display equation with original numbering
        suffix = f" {trail_text}" if trail_text else ""
        if label:
            result.append("")
            result.append(label)
            result.append(f"\\begin{{equation*}}\\tag{{{eq_num}}}")
            result.append(f"  {math_body}")
            result.append("\\end{equation*}")
            if suffix.strip():
                result.append(suffix.strip())
            result.append("")
        else:
            result.append("")
            result.append(f"\\begin{{equation*}}\\tag{{{eq_num}}}")
            result.append(f"  {math_body}")
            result.append("\\end{equation*}")
            if suffix.strip():
                result.append(suffix.strip())
            result.append("")

    return "\n".join(result)


def _wrap_math_lines(text: str) -> str:
    """
    Detect standalone math-heavy lines and wrap them in $...$ if not already wrapped.

    These are short lines (< 120 chars) that look like equations but were extracted
    as plain text. Examples:
        fs(X) - fs(X) =
        L2(fk) = max
        = sin(xn) sin(xn)⊤
        ||flatten(sin(x) sin(x)⊤)||2

    Heuristics:
    - Line is short (not a full paragraph)
    - Contains math operators (=, +, -, ||, ≤, ≥)
    - Contains function notation: f(x), sin(), max, min, log, exp
    - High ratio of single-letter variables, digits, and operators vs prose words
    - Already partially contains $...$ math spans
    """
    if not text:
        return text

    lines = text.split("\n")
    result = []

    for line in lines:
        stripped = line.strip()

        # Skip empty, already fully wrapped, or too long lines
        if (not stripped or len(stripped) > 120 or
            stripped.startswith("$") and stripped.endswith("$") or
            stripped.startswith("\\begin")):
            result.append(line)
            continue

        # Skip if already mostly in math mode
        dollar_count = stripped.count("$")
        if dollar_count >= 4:
            result.append(line)
            continue

        # Count math indicators
        has_equals = "=" in stripped
        has_parens = "(" in stripped and ")" in stripped
        has_func = bool(re.search(r'\b(sin|cos|tan|log|exp|max|min|lim|sup|inf|Pr|arg)\s*[(\[]', stripped))
        has_norm = "||" in stripped or "‖" in stripped
        has_operators = bool(re.search(r'[+\-*/=<>≤≥≪≫∈∀∃∑∫∏]', stripped))
        has_math_dollar = "$" in stripped

        # Count words vs math tokens
        words = stripped.split()
        short_tokens = sum(1 for w in words if len(w) <= 2)
        real_words = count_real_words(words)

        # Math line detection: short line with math patterns and few real English words
        is_math_line = (
            len(stripped) < 80 and
            len(real_words) <= 1 and
            has_equals and
            (has_func or has_norm or has_parens or has_math_dollar) and
            short_tokens >= len(words) * 0.3
        )

        # Also detect continuation lines: start with = or operator
        is_continuation = (
            len(stripped) < 80 and
            len(real_words) == 0 and
            re.match(r'^[=<>≤≥+\-]', stripped) and
            has_operators
        )

        if is_math_line or is_continuation:
            # Don't double-wrap parts already in $...$
            if "$" not in stripped:
                result.append(f"${stripped}$")
            else:
                result.append(line)
        else:
            result.append(line)

    return "\n".join(result)


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


# ── Equation-context implicit subscripts ────────────────────────
# ── Greek-subscript patterns ────────────────────────────────────
# Patterns like  ασ(1)π(1)  →  $\alpha_{\sigma(1)\pi(1)}$
# These appear in permutation/matrix contexts where Greek letters
# with parenthesized arguments act as subscripts of a base variable.

# Greek chars used as subscript functions (σ, π are most common)
_GREEK_CHARS = set("αβγδεζηθικλμνξπρσςτυφχψωΓΔΘΛΞΠΣΦΨΩ")
_LATIN_AND_GREEK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") | _GREEK_CHARS

# Pattern: base letter (Latin/Greek) + one or more Greek(arg) subscripts
# e.g., ασ(1)π(1), aσ(i)π(j), ασ(i), Aπ(k)
# Allows optional whitespace between base and first subscript (PDF extraction artifact)
_GREEK_SUBSCRIPT_RE = re.compile(
    r"(?<![a-zA-Z])"                        # base must NOT be part of a word
    r"([a-zA-Zαβγδεζηθικλμνξπρσςτυφχψω"
    r"ΓΔΘΛΞΠΣΦΨΩ])"                        # base letter (Latin or Greek)
    r"\s?"                                  # optional space (extraction artifact)
    r"((?:[αβγδεζηθικλμνξπρσςτυφχψω"
    r"ΓΔΘΛΞΠΣΦΨΩ]\([^)]{1,10}\)){1,4})"    # 1-4 Greek(arg) subscripts
)


def _fix_greek_subscript_patterns(text: str) -> str:
    """
    Convert Greek-subscript patterns to proper LaTeX subscript notation.

    Input:  ασ(1)π(1)
    Output: $\alpha_{\sigma(1)\pi(1)}$

    Input:  ασ(i)
    Output: $\alpha_{\sigma(i)}$

    These patterns appear in permutation/matrix contexts (common in
    combinatorics and optimization papers).

    MUST run before individual Greek→LaTeX replacement, otherwise σ becomes
    $\sigma$ and the subscript structure is lost.
    """
    if not text:
        return text

    # Quick check: need at least one Greek char followed by (
    # preceded by a standalone letter (not part of a longer word)
    has_pattern = False
    for i, ch in enumerate(text):
        if ch in _GREEK_CHARS and i + 1 < len(text) and text[i + 1] == "(":
            # Check for standalone base letter before this Greek(arg) pattern
            # Must be a single letter, NOT the tail of a multi-letter word
            if i > 0 and text[i - 1] in _LATIN_AND_GREEK:
                # The base letter must itself NOT be preceded by another letter
                if i < 2 or text[i - 2] not in _LATIN_AND_GREEK:
                    has_pattern = True
                    break
            elif (i > 1 and text[i - 1] == " "
                  and text[i - 2] in _LATIN_AND_GREEK):
                # Space between base and Greek — base must be standalone
                if i < 3 or text[i - 3] not in _LATIN_AND_GREEK:
                    has_pattern = True
                    break
    if not has_pattern:
        return text

    def _replace_greek_sub(m):
        base = m.group(1)
        subscript_part = m.group(2)

        # Convert base to LaTeX
        base_latex = GREEK_TO_LATEX.get(base, base)

        # Convert each Greek letter in the subscript part
        sub_latex = subscript_part
        for char, cmd in GREEK_TO_LATEX.items():
            sub_latex = sub_latex.replace(char, cmd)

        return f"${base_latex}_{{{sub_latex}}}$"

    text = _GREEK_SUBSCRIPT_RE.sub(_replace_greek_sub, text)
    return text


# Patterns like  aij + ajk ≤ aij + aki  →  $a_{ij}$ + $a_{jk}$ $\leq$ $a_{ij}$ + $a_{ki}$
# Also handles: A22 (matrix element), σ(1)π(1) patterns
#
# These are single uppercase/lowercase letters followed by 2-3 lowercase letters/digits
# that represent matrix subscripts, BUT only in equation-context lines
# (lines containing math operators like ≤, ≥, =, +, ×, etc.)

_IMPLICIT_SUB_RE = re.compile(
    r"(?<![a-zA-Z])"           # not preceded by a letter (avoids mid-word matches)
    r"([a-zA-Z])"              # single letter base (the variable)
    r"([ijklmnpqrs]{2,3})"     # 2-3 common subscript index letters
    r"(?![a-zA-Z])"            # not followed by more letters (avoids real words)
)

# Also handle uppercase letter + digits like A22 → $A_{22}$
_IMPLICIT_DIGIT_SUB_RE = re.compile(
    r"(?<![a-zA-Z])"           # not preceded by letter
    r"([A-Za-z])"              # single letter base
    r"(\d{1,3})"               # 1-3 digit subscript
    r"(?![a-zA-Z0-9])"         # not followed by alphanumeric
)

# Single-letter subscript: ti → $t_i$, xn → $x_n$ — very conservative
# Only matches: variable letter + single subscript letter from {i,j,k,n,m,p,q}
# Requires STRONG equation context (line has "= digit" assignment pattern)
_SINGLE_SUB_RE = re.compile(
    r"(?<![a-zA-Z])"           # not preceded by letter
    r"([a-zA-Z])"              # single letter base
    r"([ijknmpq])"             # single subscript index letter
    r"(?![a-zA-Z])"            # not followed by more letters
)
# Words that could be false-matched by _SINGLE_SUB_RE
_SINGLE_SUB_EXCLUDE = {"an", "am", "in", "on", "up", "if", "is", "it", "ok",
                        "no", "so", "to", "we", "he", "me", "be", "or", "at",
                        "of", "as", "by", "do", "go", "hi", "mi"}

# Words that look like subscript patterns but aren't (English words)
_IMPLICIT_SUB_EXCLUDE = {
    "aim", "air", "all", "ami", "ask", "nil", "sir", "ski", "slim",
    "sin", "kin", "pin", "rim", "pis", "sim", "lip", "rip", "sip",
    "inn", "ink", "ill", "iris", "mini", "kiss", "miss", "risk",
    "slim", "skip", "skin", "spin", "slip", "grin", "grip", "trim",
    "trip", "milk", "silk", "kill", "fill", "pill", "till", "will",
    "mill", "hill", "bill", "nil", "Jim", "Kim", "Tim",
}

# Math context indicators — line must contain at least one
_MATH_CONTEXT_CHARS = set("≤≥≠≈∈∉⊂⊃∪∩∧∨¬∀∃∅∇∂√∫∑∏±×÷→←↔⊤⊥⊕⊗∼")


def _fix_implicit_subscripts(text: str) -> str:
    """
    Detect implicit matrix subscripts in equation-context lines.

    'aij + ajk ≤ aij + aki' → '$a_{ij}$ + $a_{jk}$ $\\leq$ $a_{ij}$ + $a_{ki}$'

    Only applies to lines that contain math operators (≤, =, +, etc. with
    at least one non-ASCII math symbol or already-converted $...$ math),
    preventing false positives on English prose.
    """
    if not text:
        return text

    lines = text.split("\n")
    result = []

    for line in lines:
        stripped = line.strip()

        # Check for equation context: must have math operators or existing $...$ math
        has_math_context = bool(_MATH_CONTEXT_CHARS & set(stripped))
        has_dollar_math = "$" in stripped
        has_eq_sign_with_vars = bool(re.search(r'[a-z]\s*[+\-=<>]\s*[a-z]', stripped))
        has_assignment = bool(re.search(r'=\s*\d', stripped))

        # Need at least one strong math indicator AND some operator
        if not (has_math_context or has_assignment
                or (has_dollar_math and has_eq_sign_with_vars)):
            result.append(line)
            continue

        # Apply implicit letter-subscript pattern (aij → $a_{ij}$)
        def _replace_implicit(m):
            full = m.group(0)
            if full.lower() in _IMPLICIT_SUB_EXCLUDE:
                return full  # it's an English word, leave it
            base = m.group(1)
            sub = m.group(2)
            return f"${base}_{{{sub}}}$"

        new_line = _apply_outside_math(line, _IMPLICIT_SUB_RE, _replace_implicit)

        # Apply digit-subscript pattern in equation context (A22 → $A_{22}$)
        # Only when near math context, not arbitrary numbers like "page 22"
        if has_math_context:
            def _replace_digit_sub(m):
                base = m.group(1)
                digits = m.group(2)
                return f"${base}_{{{digits}}}$"
            new_line = _apply_outside_math(new_line, _IMPLICIT_DIGIT_SUB_RE, _replace_digit_sub)

        # Apply single-letter subscript in STRONG equation context only
        # "ti = 0 a tj = 1" → "$t_i$ = 0 a $t_j$ = 1"
        # Requires "= digit" pattern (assignment-like) to avoid false positives
        if has_assignment or has_math_context:
            def _replace_single_sub(m):
                full = m.group(0).lower()
                if full in _SINGLE_SUB_EXCLUDE:
                    return m.group(0)
                base = m.group(1)
                sub = m.group(2)
                return f"${base}_{{{sub}}}$"
            new_line = _apply_outside_math(new_line, _SINGLE_SUB_RE, _replace_single_sub)

        result.append(new_line)

    return "\n".join(result)


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


def _fix_math_primes(text: str) -> str:
    """
    Fix double-superscript errors caused by ' (prime) characters inside $...$ blocks.

    In LaTeX math mode, ' is ^\prime. Two primes in a row ('') or a prime next to
    another superscript ('^{2}') triggers "Double superscript" errors.

    Fixes:
    - $' '$ → removed (empty prime pair is meaningless)
    - $' \times '$ → $\times$ (stray primes around operators)
    - $...' '...$ → $...\prime ...$ (double prime → single prime notation)
    - $'$ at start/end of text → removed (stray primes)
    - ' immediately before $ or after $ → removed (quotes at math boundary)
    """
    # Fix 1: Remove stray ' immediately before $ (start of math) or after $ (end of math)
    # ' $ ... → just remove the stray quote
    text = re.sub(r"'\s*\$", "$", text)
    # ...$ ' → just remove the stray quote
    text = re.sub(r"\$\s*'", "$", text)

    # Fix 2: Inside $...$ blocks, fix prime issues
    def _fix_primes_in_math(m):
        content = m.group(1)
        # Remove isolated ' that aren't attached to a variable (letter/digit before ')
        # Good: x' (variable with prime) → keep
        # Bad: ' \times ' (stray primes) → remove
        # Replace stray leading/trailing primes
        content = re.sub(r"^'\s*", "", content)  # leading prime
        content = re.sub(r"\s*'$", "", content)  # trailing prime
        # Replace ' ' (two isolated primes) with nothing
        content = re.sub(r"'\s+'", "", content)
        # Replace stray ' not after a letter/digit (not variable primes)
        content = re.sub(r"(?<![a-zA-Z0-9])'\s*", "", content)

        if not content.strip():
            return ""
        return f"${content}$"

    text = re.sub(r"\$([^$]+)\$", _fix_primes_in_math, text)

    # Fix 3: Clean up empty $$ or $ $ left over
    text = re.sub(r"\$\s*\$", "", text)

    return text


def _merge_adjacent_math(text: str) -> str:
    """
    Merge adjacent $...$<op>$...$ into single $...<op>...$ so LaTeX renders
    one continuous equation instead of fragmented inline math.

    $a^{2}$ + $b^{2}$ = $c^{2}$  →  $a^{2} + b^{2} = c^{2}$
    $a^{2}$$b^{2}$                →  $a^{2} b^{2}$
    $\\lambda$(A)                 →  $\\lambda(A)$
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

    # Step 3: Absorb trailing (args) into math mode for function-like patterns.
    # $\lambda$(A)  →  $\lambda(A)$
    # $\sigma$(i)   →  $\sigma(i)$
    # This handles Greek letter function calls where the argument was not wrapped.
    text = re.sub(
        r"\$(\\\w+)\$(\([^)]{1,20}\))",
        r"$\1\2$",
        text,
    )

    return text


def _strip_unsafe_unicode(text: str) -> str:
    """Remove Unicode chars that pdflatex can't handle (Private Use Area, combining, etc.)."""
    result = []
    for ch in text:
        cp = ord(ch)
        # Keep ASCII
        if cp < 128:
            result.append(ch)
        # Keep common Latin Extended (accented chars)
        elif cp < 0x0250:
            result.append(ch)
        # Skip combining characters (U+0300..U+036F) — these crash pdflatex
        elif 0x0300 <= cp <= 0x036F:
            continue
        # Keep if it's a known LaTeX-wrapped symbol (already converted above)
        elif ch in GREEK_TO_LATEX or ch in MATH_SYMBOLS:
            result.append(ch)
        # Skip Private Use Area (U+E000..U+F8FF) — these crash pdflatex
        elif 0xE000 <= cp <= 0xF8FF:
            continue
        # Skip U+FFFD replacement character — undecodable chars from PDF extraction
        # These render as (cid:XX) garbage in pdflatex output
        elif cp == 0xFFFD:
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
            # Don't double-wrap values that already contain $...$
            if cmd.startswith("$") and cmd.endswith("$"):
                text = text.replace(char, cmd)
            else:
                text = text.replace(char, f"${cmd}$")

    # Step 5: Merge adjacent math fragments
    text = _merge_adjacent_math(text)

    # Step 6: Strip unsafe unicode
    text = _strip_unsafe_unicode(text)

    return text
