"""
canon/features.py — 16-feature vector per line (foundation for ML classifier).

Each line of extracted text gets a feature vector that can be used by
the optional sklearn classifier or for heuristic scoring.
"""
import re
from typing import List


def extract_features(line: str, font_size: float = 0, body_size: float = 0) -> List[float]:
    """
    Compute a 16-feature vector for a single text line.

    Features:
     0: line length (chars)
     1: word count
     2: starts with uppercase (0/1)
     3: all uppercase (0/1)
     4: has digits (0/1)
     5: starts with digit (0/1)
     6: has special chars ratio
     7: ends with period (0/1)
     8: matches numbered heading pattern (0/1)
     9: matches keyword heading (0/1)
    10: has email (0/1)
    11: has URL (0/1)
    12: font size ratio (to body size)
    13: short line (<50 chars, 0/1)
    14: has brackets (0/1)
    15: has math chars (0/1)
    """
    stripped = line.strip()
    features = [0.0] * 16

    if not stripped:
        return features

    features[0] = len(stripped)
    features[1] = len(stripped.split())
    features[2] = 1.0 if stripped[0].isupper() else 0.0
    features[3] = 1.0 if stripped.isupper() else 0.0
    features[4] = 1.0 if any(c.isdigit() for c in stripped) else 0.0
    features[5] = 1.0 if stripped[0].isdigit() else 0.0

    special = sum(1 for c in stripped if not c.isalnum() and c != " ")
    features[6] = special / max(len(stripped), 1)

    features[7] = 1.0 if stripped.endswith(".") else 0.0
    features[8] = 1.0 if re.match(r"^\d+\.?\s+\w+", stripped) else 0.0
    features[9] = 1.0 if stripped.lower() in {
        "abstract", "introduction", "conclusion", "references",
        "methods", "results", "discussion", "background"
    } else 0.0

    features[10] = 1.0 if re.search(r"[\w.+-]+@[\w-]+\.\w+", stripped) else 0.0
    features[11] = 1.0 if re.search(r"https?://", stripped) else 0.0

    if body_size > 0 and font_size > 0:
        features[12] = font_size / body_size
    else:
        features[12] = 1.0

    features[13] = 1.0 if len(stripped) < 50 else 0.0
    features[14] = 1.0 if "[" in stripped or "]" in stripped else 0.0

    math_chars = "αβγδεζηθικλμνξπρστυφχψω∑∫∂∇±∞≈≠≤≥"
    features[15] = 1.0 if any(c in stripped for c in math_chars) else 0.0

    return features
