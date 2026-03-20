"""
canon/classifier.py — Optional ML line classifier (sklearn-based)

This module is OPTIONAL.  The builder.py works without it using rule-based
scoring from features.py.  Add this when you've collected enough labeled data
from your test papers (aim for 200-500 labeled lines).

HOW TO USE:
  Step 1: Generate a labeling CSV from your papers:
          python -m canon.classifier label --input extracted.txt --output labels.csv

  Step 2: Manually correct the auto-labels in labels.csv in Excel/VSCode.
          Labels: heading | title | author | abstract | body | reference | metadata

  Step 3: Train the model:
          python -m canon.classifier train --labels labels.csv --output model.pkl

  Step 4: Enable in builder.py by setting USE_ML_CLASSIFIER = True in config.py

CLASSES:
  - "heading"   : section heading line
  - "title"     : paper title line
  - "author"    : author name or affiliation
  - "abstract"  : abstract text line
  - "body"      : regular body paragraph text
  - "reference" : bibliography entry
  - "metadata"  : page header, DOI, copyright, etc.

MODEL:
  Using sklearn LinearSVC (fast, interpretable, works well on small datasets).
  Features come from canon/features.py (16 numeric features).
  A properly labeled dataset of ~300 lines gets ~85-90% accuracy.
"""
import os
import csv
import pickle
import logging
from typing import List, Tuple, Optional
from canon.features import extract_features

log = logging.getLogger(__name__)

# Path where the trained model is saved
MODEL_PATH = os.path.join(os.path.dirname(__file__), "line_classifier.pkl")

CLASSES = ["heading", "title", "author", "abstract", "body", "reference", "metadata"]


# ══════════════════════════════════════════════════════════════════════════════
# Inference
# ══════════════════════════════════════════════════════════════════════════════

class LineClassifier:
    """
    Wraps the trained sklearn model.  Falls back to rule-based scoring
    from features.py if the model file doesn't exist.
    """

    def __init__(self):
        self._model = None
        self._load()

    def _load(self):
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)
                log.info("ML classifier loaded from %s", MODEL_PATH)
            except Exception as e:
                log.warning("Failed to load ML classifier: %s", e)
                self._model = None
        else:
            log.debug("No ML classifier found at %s — using rule-based fallback", MODEL_PATH)

    @property
    def is_available(self) -> bool:
        return self._model is not None

    def predict(self, line: str) -> Tuple[str, float]:
        """
        Predict the class of a single line.
        Returns (class_label, confidence_0_to_1).
        Falls back to rule-based if model unavailable.
        """
        if not self._model:
            return self._rule_based_predict(line)

        feats = [extract_features(line)]
        label = self._model.predict(feats)[0]

        # Get confidence from decision function if available
        try:
            scores = self._model.decision_function(feats)[0]
            # Normalize to 0-1 range using softmax-like approach
            import math
            exp_scores = [math.exp(s - max(scores)) for s in scores]
            total = sum(exp_scores)
            idx = list(self._model.classes_).index(label)
            confidence = exp_scores[idx] / total
        except Exception:
            confidence = 0.7  # Default if we can't get decision scores

        return label, confidence

    def _rule_based_predict(self, line: str) -> Tuple[str, float]:
        """Fallback rule-based classifier using feature scoring."""
        from canon.features import heading_score, title_score, author_score, reference_score
        feats = extract_features(line)

        scores = {
            "heading":   heading_score(feats),
            "title":     title_score(feats),
            "author":    author_score(feats),
            "reference": reference_score(feats),
            "body":      0.3,  # baseline — most lines are body text
        }

        # Check for metadata indicators
        line_lower = line.lower()
        if any(kw in line_lower for kw in ["doi", "http", "©", "copyright", "received:", "published:"]):
            scores["metadata"] = 0.9

        best_class = max(scores, key=lambda k: scores[k])
        return best_class, scores[best_class]


# ══════════════════════════════════════════════════════════════════════════════
# Training utilities
# ══════════════════════════════════════════════════════════════════════════════

def auto_label_file(text: str) -> List[dict]:
    """
    Auto-label lines from extracted text using rule-based heuristics.
    Output is a list of dicts with keys: line, auto_label, confidence.
    You then manually correct the auto_label column in the CSV.
    """
    from canon.features import heading_score, title_score, author_score, reference_score

    results = []
    lines = text.splitlines()
    in_refs = False

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Check for references section
        if re.match(r'^(References|Bibliography)\s*$', line, re.IGNORECASE):
            in_refs = True

        feats = extract_features(line)

        if in_refs and feats[13] > 0.5:  # bracket_number_start
            label, conf = "reference", 0.85
        elif i < 5:
            # First few lines likely title/author
            ts = title_score(feats)
            als = author_score(feats)
            if ts > als:
                label, conf = "title", ts
            else:
                label, conf = "author", als
        else:
            hs = heading_score(feats)
            if hs > 0.5:
                label, conf = "heading", hs
            else:
                label, conf = "body", 0.5

        results.append({"line": line, "auto_label": label, "confidence": round(conf, 2)})

    return results


def save_labels_csv(labeled_lines: List[dict], path: str):
    """Save auto-labeled lines to CSV for manual correction."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["line", "auto_label", "confidence", "corrected_label"])
        writer.writeheader()
        for row in labeled_lines:
            writer.writerow({**row, "corrected_label": row["auto_label"]})
    print(f"Saved {len(labeled_lines)} lines to {path}")
    print(f"Edit 'corrected_label' column, then run: python -m canon.classifier train --labels {path}")


def train_from_csv(labels_csv: str, model_output: str = MODEL_PATH):
    """
    Train a LinearSVC classifier from a manually corrected labels CSV.
    Requires: pip install scikit-learn
    """
    try:
        from sklearn.svm import LinearSVC
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        from sklearn.metrics import classification_report
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("ERROR: scikit-learn not installed. Run: pip install scikit-learn")
        return

    import re

    rows = []
    with open(labels_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row.get("corrected_label") or row.get("auto_label")
            if label and label in CLASSES:
                rows.append((row["line"], label))

    if len(rows) < 30:
        print(f"WARNING: Only {len(rows)} labeled rows — need at least 30 for reliable training")
        return

    X = [extract_features(line) for line, _ in rows]
    y = [label for _, label in rows]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LinearSVC(max_iter=2000, C=1.0))
    ])
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    print(f"\nTrained on {len(X_train)} samples, tested on {len(X_test)}")
    print(classification_report(y_test, y_pred, zero_division=0))

    with open(model_output, "wb") as f:
        pickle.dump(model, f)
    print(f"Model saved to {model_output}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI entrypoint: python -m canon.classifier
# ══════════════════════════════════════════════════════════════════════════════

import re

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="Canon line classifier tools")
    sub = parser.add_subparsers(dest="cmd")

    lbl = sub.add_parser("label", help="Auto-label lines from extracted text")
    lbl.add_argument("--input", required=True, help="Path to extracted.txt")
    lbl.add_argument("--output", default="labels.csv", help="Output CSV path")

    trn = sub.add_parser("train", help="Train model from corrected CSV")
    trn.add_argument("--labels", required=True, help="Path to corrected labels CSV")
    trn.add_argument("--output", default=MODEL_PATH, help="Output model .pkl path")

    tst = sub.add_parser("predict", help="Predict class for a line")
    tst.add_argument("--line", required=True, help="Line to classify")

    args = parser.parse_args()

    if args.cmd == "label":
        with open(args.input, "r", encoding="utf-8") as f:
            text = f.read()
        labeled = auto_label_file(text)
        save_labels_csv(labeled, args.output)

    elif args.cmd == "train":
        train_from_csv(args.labels, args.output)

    elif args.cmd == "predict":
        clf = LineClassifier()
        label, conf = clf.predict(args.line)
        print(f"  Line    : {args.line!r}")
        print(f"  Predicted : {label}  (confidence: {conf:.2f})")
        print(f"  ML available: {clf.is_available}")

    else:
        parser.print_help()