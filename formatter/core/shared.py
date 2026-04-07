"""
core/shared.py — Shared utilities to eliminate cross-module duplication.

Consolidates: MATH_CHARS, real-word detection, OCR threshold, path helpers.
"""
import os
import re

# ── Canonical MATH_CHARS set (used by extractor, normalizer, and parser) ────
# Single source of truth — never redefine this elsewhere.
MATH_CHARS = set()
for _c in range(0x0391, 0x03C9 + 1): MATH_CHARS.add(chr(_c))   # Greek
for _c in range(0x2200, 0x22FF + 1): MATH_CHARS.add(chr(_c))   # Math operators
for _c in range(0x2190, 0x21FF + 1): MATH_CHARS.add(chr(_c))   # Arrows
for _c in range(0x2070, 0x209F + 1): MATH_CHARS.add(chr(_c))   # Super/subscript digits
MATH_CHARS.update("±×÷∞≈≠≤≥∈∉⊂⊃∪∩∧∨¬∀∃∅∇∂√∫∑∏")
# Variant Greek forms
MATH_CHARS.update("ϕϵϑϱϖ")
# Additional symbols used in normalizer but missing from extractor
MATH_CHARS.update("∼⊤⊥⊕⊗∘⟨⟩‖′″∝≡≅≪≫")


# ── OCR confidence thresholds ───────────────────────────────────────
OCR_CONFIDENCE_THRESHOLD = 0.60    # standalone formula blocks
TABLE_CELL_OCR_THRESHOLD = 0.40    # table cells (lower = prefer selectable text)
OCR_RENDERER_THRESHOLD = 0.80      # renderer-side gate for latex vs image


# ── "Real words" detection ──────────────────────────────────────────
# Canonical stop-word set for math-context word filtering.
# Words that appear in equations/formulas but aren't "real prose words".
MATH_STOP_WORDS = frozenset({
    "true", "false", "sin", "cos", "tan", "log", "exp",
    "max", "min", "flatten", "terms", "where", "with",
    "prior", "posterior", "prediction", "likelihood",
    "that", "this",
})


def count_real_words(words: list, extra_exclude: set = None, min_len: int = 4) -> list:
    """
    Filter a list of word tokens to find 'real' English words.

    Excludes:
    - Words shorter than min_len
    - Non-alpha words
    - Known math/equation stop words
    - Any additional exclusions passed via extra_exclude

    Returns list of real words (not just count, so caller can inspect).
    """
    exclude = MATH_STOP_WORDS
    if extra_exclude:
        exclude = exclude | extra_exclude
    return [w for w in words if w.isalpha() and len(w) >= min_len
            and w.lower() not in exclude]


# ── LaTeX path relativization ───────────────────────────────────────

def latex_relpath(abs_path: str, base_dir: str) -> str:
    """
    Convert an absolute file path to a LaTeX-safe relative path.

    - Converts to relative path from base_dir
    - Normalizes backslashes to forward slashes (LaTeX requirement)
    - Falls back to absolute path on cross-drive (Windows) errors
    """
    if not abs_path:
        return ""
    result = abs_path
    if os.path.isabs(abs_path):
        try:
            result = os.path.relpath(abs_path, base_dir)
        except ValueError:
            pass  # different drive on Windows — keep absolute
    return result.replace("\\", "/")
