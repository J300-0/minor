"""
extractor/pix2tex_worker.py — Isolated pix2tex OCR worker.

Called by pdf_extractor.py via subprocess:
    python pix2tex_worker.py <image_path>

Prints the LaTeX result to stdout (one line).
Exits 0 on success, 1 on failure.

Running in a subprocess means any segfault, sys.exit(), or CUDA crash
inside pix2tex stays isolated and never kills the main pipeline.
"""
import os
import sys


def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    image_path = sys.argv[1]
    if not os.path.exists(image_path):
        print(f"File not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    try:
        from pix2tex.cli import LatexOCR
        from PIL import Image

        model = LatexOCR()
        img = Image.open(image_path)
        result = model(img)

        if result and len(result.strip()) >= 2:
            print(result.strip())
            sys.exit(0)
        else:
            sys.exit(1)

    except Exception as e:
        print(f"pix2tex error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
