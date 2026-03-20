"""
canon/classifier.py — Optional ML line classifier (sklearn-based).

This module is OPTIONAL.  builder.py works without it using rule-based scoring.
Add this when you've collected labeled data (aim for 200-500 labeled lines).

HOW TO USE:
  Step 1: python -m canon.classifier label --input extracted.txt --output labels.csv
  Step 2: Correct auto_label in labels.csv (corrected_label column)
  Step 3: python -m canon.classifier train --labels labels.csv --output model.pkl
  Step 4: python -m canon.classifier predict --line "2. Related Work"

CLASSES:
  heading | title | author | abstract | body | reference | metadata
"""
import csv
import logging
import math
import os
import pickle
import re
from typing import List, Tuple

from canon.features import (
    extract_features, heading_score, title_score,
    author_score, reference_score,
)

log = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "line_classifier.pkl")
CLASSES = ["heading", "title", "author", "abstract", "body", "reference", "metadata"]


# ══════════════════════════════════════════════════════════════════════════════
# Inference
# ══════════════════════════════════════════════════════════════════════════════

class LineClassifier:
    """Wraps sklearn model. Falls back to rule-based if model not found."""

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
            log.debug("No ML classifier at %s — using rule-based fallback", MODEL_PATH)

    @property
    def is_available(self) -> bool:
        return self._model is not None

    def predict(self, line: str) -> Tuple[str, float]:
        """Predict class of a single line. Returns (label, confidence)."""
        if not self._model:
            return self._rule_based_predict(line)

        feats = [extract_features(line)]
        label = self._model.predict(feats)[0]

        try:
            scores = self._model.decision_function(feats)[0]
            exp_scores = [math.exp(s - max(scores)) for s in scores]
            total = sum(exp_scores)
            idx = list(self._model.classes_).index(label)
            confidence = exp_scores[idx] / total
        except Exception:
            confidence = 0.7

        return label, confidence

    def _rule_based_predict(self, line: str) -> Tuple[str, float]:
        feats = extract_features(line)

        scores = {
            "heading":   heading_score(feats),
            "title":     title_score(feats),
            "author":    author_score(feats),
            "reference": reference_score(feats),
            "body":      0.3,
        }

        line_lower = line.lower()
        if any(kw in line_lower for kw in [
            "doi", "http", "\u00a9", "copyright", "received:", "published:",
        ]):
            scores["metadata"] = 0.9

        best_class = max(scores, key=lambda k: scores[k])
        return best_class, scores[best_class]


# ══════════════════════════════════════════════════════════════════════════════
# Training utilities
# ══════════════════════════════════════════════════════════════════════════════

def auto_label_file(text: str) -> List[dict]:
    """Auto-label lines using rule-based heuristics."""
    results = []
    lines = text.splitlines()
    in_refs = False

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        if re.match(r"^(References|Bibliography)\s*$", line, re.IGNORECASE):
            in_refs = True

        feats = extract_features(line)

        if in_refs and feats[13] > 0.5:
            label, conf = "reference", 0.85
        elif i < 5:
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

        results.append({
            "line": line,
            "auto_label": label,
            "confidence": round(conf, 2),
        })

    return results


def save_labels_csv(labeled_lines: List[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["line", "auto_label", "confidence", "corrected_label"],
        )
        writer.writeheader()
        for row in labeled_lines:
            writer.writerow({**row, "corrected_label": row["auto_label"]})
    print(f"Saved {len(labeled_lines)} lines to {path}")


def train_from_csv(labels_csv: str, model_output: str = MODEL_PATH):
    """Train LinearSVC from corrected labels CSV."""
    try:
        from sklearn.svm import LinearSVC
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        from sklearn.metrics import classification_report
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("ERROR: scikit-learn not installed. Run: pip install scikit-learn")
        return

    rows = []
    with open(labels_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row.get("corrected_label") or row.get("auto_label")
            if label and label in CLASSES:
                rows.append((row["line"], label))

    if len(rows) < 30:
        print(f"WARNING: Only {len(rows)} labeled rows — need >= 30")
        return

    X = [extract_features(line) for line, _ in rows]
    y = [label for _, label in rows]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42,
    )

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LinearSVC(max_iter=2000, C=1.0)),
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

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Canon line classifier tools")
    sub = parser.add_subparsers(dest="cmd")

    lbl = sub.add_parser("label", help="Auto-label lines from extracted text")
    lbl.add_argument("--input", required=True, help="Path to extracted.txt")
    lbl.add_argument("--output", default="labels.csv", help="Output CSV path")

    trn = sub.add_parser("train", help="Train model from corrected CSV")
    trn.add_argument("--labels", required=True, help="Corrected labels CSV")
    trn.add_argument("--output", default=MODEL_PATH, help="Output .pkl path")

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
        print(f"  Line      : {args.line!r}")
        print(f"  Predicted : {label}  (confidence: {conf:.2f})")
        print(f"  ML available: {clf.is_available}")
    else:
        parser.print_help()
