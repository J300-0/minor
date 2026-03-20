"""
canon/features.py — Extract numeric features from a text line.

This is the foundation for the optional ML classifier (canon/classifier.py).
Even without ML, these features are used by the rule-based scorer in builder.py
to assign confidence scores to each extracted field.

Feature vector (16 features, all floats in [0,1] unless noted):
  0  line_length_norm     length / 200 (clamped to 1.0)
  1  alpha_ratio          fraction of chars that are alphabetic
  2  digit_ratio          fraction of chars that are digits
  3  upper_ratio          fraction of alpha chars that are uppercase
  4  starts_digit         1 if first non-space char is a digit
  5  starts_upper         1 if first alpha char is uppercase
  6  ends_colon           1 if line ends with ':'
  7  ends_period          1 if line ends with '.'
  8  has_email            1 if '@' present
  9  has_url              1 if 'http' present
  10 word_count_norm      word count / 30 (clamped to 1.0)
  11 known_section_word   1 if line (lowercased) contains a known section keyword
  12 roman_numeral_start  1 if line starts with I/II/III/IV/V etc.
  13 bracket_number_start 1 if line starts with [N] (reference marker)
  14 all_caps             1 if line is all uppercase letters (ignoring spaces)
  15 short_line           1 if word count <= 6 (candidate heading)
"""
import re
from typing import List

# Section keywords that commonly appear as headings in academic papers
_SECTION_KEYWORDS = {
    "abstract", "introduction", "background", "related work", "related",
    "methodology", "methods", "method", "approach", "proposed",
    "experiments", "experiment", "experimental", "evaluation", "results",
    "discussion", "analysis", "conclusion", "conclusions", "future work",
    "acknowledgment", "acknowledgements", "references", "appendix",
    "literature", "overview", "framework", "architecture", "implementation",
    "performance", "dataset", "datasets", "training", "model", "models",
    "system", "setup", "settings", "baseline", "comparison",
}

_ROMAN = re.compile(r"^(I{1,3}|IV|V|VI{0,3}|IX|X{1,3})\b", re.IGNORECASE)
_BRACKET_NUM = re.compile(r"^\[\d+\]")


def extract_features(line: str) -> List[float]:
    """Return a 16-element feature vector for a single text line."""
    line = line.strip()
    n = len(line)
    if n == 0:
        return [0.0] * 16

    alpha_chars = [c for c in line if c.isalpha()]
    digit_chars = [c for c in line if c.isdigit()]
    words = line.split()

    alpha_ratio = len(alpha_chars) / n
    digit_ratio = len(digit_chars) / n
    upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / max(len(alpha_chars), 1)

    starts_digit = 1.0 if line and line[0].isdigit() else 0.0
    first_alpha = next((c for c in line if c.isalpha()), None)
    starts_upper = 1.0 if first_alpha and first_alpha.isupper() else 0.0

    ends_colon = 1.0 if line.endswith(":") else 0.0
    ends_period = 1.0 if line.endswith(".") else 0.0

    has_email = 1.0 if "@" in line else 0.0
    has_url = 1.0 if "http" in line.lower() else 0.0

    word_count_norm = min(len(words) / 30.0, 1.0)

    line_lower = line.lower()
    known_section_word = 1.0 if any(kw in line_lower for kw in _SECTION_KEYWORDS) else 0.0

    roman_numeral_start = 1.0 if _ROMAN.match(line) else 0.0
    bracket_number_start = 1.0 if _BRACKET_NUM.match(line) else 0.0

    all_caps = 1.0 if (alpha_chars and all(c.isupper() for c in alpha_chars)) else 0.0

    short_line = 1.0 if len(words) <= 6 else 0.0

    return [
        min(n / 200.0, 1.0),   # 0  line_length_norm
        alpha_ratio,            # 1  alpha_ratio
        digit_ratio,            # 2  digit_ratio
        upper_ratio,            # 3  upper_ratio
        starts_digit,           # 4  starts_digit
        starts_upper,           # 5  starts_upper
        ends_colon,             # 6  ends_colon
        ends_period,            # 7  ends_period
        has_email,              # 8  has_email
        has_url,                # 9  has_url
        word_count_norm,        # 10 word_count_norm
        known_section_word,     # 11 known_section_word
        roman_numeral_start,    # 12 roman_numeral_start
        bracket_number_start,   # 13 bracket_number_start
        all_caps,               # 14 all_caps
        short_line,             # 15 short_line
    ]


# ── Rule-based line type scoring (no ML required) ────────────────────────────
# These functions use the feature vector to score how likely a line is
# to be a heading, author line, title line, or reference line.
# Scores are in [0, 1].  Used by builder.py for confidence assignment.

def heading_score(feats: List[float]) -> float:
    """How likely is this line to be a section heading?"""
    score = 0.0
    score += feats[15] * 0.3   # short line
    score += feats[11] * 0.3   # known section keyword
    score += feats[14] * 0.1   # all caps
    score += feats[12] * 0.2   # roman numeral start
    score += feats[4]  * 0.1   # starts with digit (e.g. "2. Introduction")
    score -= feats[7]  * 0.2   # ends with period → probably body text
    score -= feats[10] * 0.3   # long word count → probably body
    return max(0.0, min(1.0, score))


def title_score(feats: List[float]) -> float:
    """How likely is this line to be the paper title?"""
    score = 0.0
    score += feats[5]  * 0.2   # starts uppercase
    score += feats[15] * 0.2   # short-ish (titles are not paragraphs)
    score += feats[1]  * 0.2   # high alpha ratio (no junk chars)
    score -= feats[2]  * 0.3   # digits → less likely title
    score -= feats[8]  * 0.5   # email → author line
    score -= feats[9]  * 0.5   # url → metadata
    return max(0.0, min(1.0, score))


def author_score(feats: List[float]) -> float:
    """How likely is this line to be an author name/affiliation?"""
    score = 0.0
    score += feats[8]  * 0.5   # has email
    score += feats[15] * 0.2   # short line
    score += feats[5]  * 0.1   # starts uppercase
    score += feats[2]  * 0.1   # some digits (superscripts, zip codes)
    return max(0.0, min(1.0, score))


def reference_score(feats: List[float]) -> float:
    """How likely is this line to be a bibliography entry?"""
    score = 0.0
    score += feats[13] * 0.6   # [N] marker
    score += feats[4]  * 0.2   # starts with digit
    score += feats[7]  * 0.1   # ends with period
    return max(0.0, min(1.0, score))