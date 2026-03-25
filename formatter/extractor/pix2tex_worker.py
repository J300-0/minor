#!/usr/bin/env python3
"""
extractor/pix2tex_worker.py — Subprocess worker for pix2tex LaTeX OCR.

Usage:
    python pix2tex_worker.py <image_path>

Prints LaTeX string to stdout.  Runs in a child process so that a
segfault (CUDA DLL issues on Windows) cannot kill the main pipeline.
"""
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: pix2tex_worker.py <image_path>", file=sys.stderr)
        sys.exit(1)

    image_path = sys.argv[1]

    try:
        from PIL import Image
        from pix2tex.cli import LatexOCR

        model = LatexOCR()
        img = Image.open(image_path)
        latex = model(img)
        print(latex, end="")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
