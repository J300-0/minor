#!/usr/bin/env python3
"""
extractor/pix2tex_batch_worker.py — Batch subprocess worker for pix2tex LaTeX OCR.

Usage:
    python pix2tex_batch_worker.py <image_path_1> <image_path_2> ...

Loads the model ONCE, then OCRs all images in sequence.
Outputs a JSON array of results to stdout:
    [{"path": "...", "latex": "..."}, ...]

Falls back gracefully: if a single image fails, its entry has latex="".
Runs in a child process so segfaults cannot kill the main pipeline.
"""
import json
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: pix2tex_batch_worker.py <image_path> [<image_path> ...]",
              file=sys.stderr)
        sys.exit(1)

    image_paths = sys.argv[1:]

    try:
        from PIL import Image
        from pix2tex.cli import LatexOCR

        # Load model ONCE — this is the expensive step (~5-10s)
        model = LatexOCR()

        results = []
        for path in image_paths:
            try:
                img = Image.open(path)
                latex = model(img)
                results.append({"path": path, "latex": latex or ""})
            except Exception as e:
                print(f"WARN: Failed on {path}: {e}", file=sys.stderr)
                results.append({"path": path, "latex": ""})

        # Output all results as JSON
        print(json.dumps(results), end="")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
