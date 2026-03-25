"""
canon/classifier.py — Optional sklearn ML classifier for line typing.

Usage:
    # Label lines
    python -m canon.classifier label --input extracted.txt --output labels.csv

    # Train
    python -m canon.classifier train --labels labels.csv --output canon/line_classifier.pkl

    # Predict
    python -m canon.classifier predict --line "2. Related Work"

You need ~200-300 labeled lines from 5+ papers for reliable results.
"""
import os
import logging

log = logging.getLogger("paper_formatter")

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "line_classifier.pkl")


class LineClassifier:
    """Wraps an optional sklearn classifier for line-type prediction."""

    def __init__(self):
        self.model = None
        self.vectorizer = None
        self._load()

    def _load(self):
        if not os.path.isfile(_MODEL_PATH):
            return
        try:
            import pickle
            with open(_MODEL_PATH, "rb") as f:
                data = pickle.load(f)
                self.model = data.get("model")
                self.vectorizer = data.get("vectorizer")
            log.info("Loaded ML line classifier from %s", _MODEL_PATH)
        except Exception as e:
            log.debug("Could not load ML classifier: %s", e)

    def is_available(self) -> bool:
        return self.model is not None

    def predict(self, features: list) -> str:
        """Predict line type from feature vector. Returns label string."""
        if not self.is_available():
            return "unknown"
        try:
            import numpy as np
            X = np.array([features])
            return self.model.predict(X)[0]
        except Exception:
            return "unknown"


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")

    label_p = sub.add_parser("label")
    label_p.add_argument("--input", required=True)
    label_p.add_argument("--output", required=True)

    train_p = sub.add_parser("train")
    train_p.add_argument("--labels", required=True)
    train_p.add_argument("--output", default=_MODEL_PATH)

    predict_p = sub.add_parser("predict")
    predict_p.add_argument("--line", required=True)

    args = parser.parse_args()

    if args.command == "label":
        from canon.features import extract_features
        import csv
        with open(args.input, "r") as inf, open(args.output, "w", newline="") as outf:
            writer = csv.writer(outf)
            writer.writerow(["line", "auto_label", "corrected_label", "features"])
            for line in inf:
                line = line.strip()
                if not line:
                    continue
                feats = extract_features(line)
                # Auto-label based on simple heuristics
                auto = "body"
                if len(line) < 50 and line[0].isupper():
                    auto = "heading"
                writer.writerow([line, auto, "", str(feats)])
        print(f"Labels written to {args.output}")

    elif args.command == "predict":
        from canon.features import extract_features
        clf = LineClassifier()
        if not clf.is_available():
            print("No trained model found. Train first.")
            sys.exit(1)
        feats = extract_features(args.line)
        label = clf.predict(feats)
        print(f"Predicted: {label}")

    else:
        parser.print_help()
