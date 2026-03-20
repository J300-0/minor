"""
run_pipeline.py — CLI entry point
Usage: python run_pipeline.py path/to/input.pdf
"""

import sys
from core.pipeline import run

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py <input.pdf|input.docx>")
        sys.exit(1)
    run(sys.argv[1])