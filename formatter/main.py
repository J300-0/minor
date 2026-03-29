"""
main.py — Paper Formatter CLI

Usage:
    python main.py input/paper.pdf
    python main.py input/paper.pdf --template acm
    python main.py input/paper.docx --template ieee
    python main.py input/paper.pdf --template springer --output my_output/

Available templates: ieee, acm, springer, elsevier, apa, arxiv
"""
import argparse
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.pipeline import run
from core.config import TEMPLATE_REGISTRY, DEFAULT_TEMPLATE


def main():
    parser = argparse.ArgumentParser(
        description="Format academic papers into IEEE/ACM/Springer/Elsevier/APA/arXiv PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="Path to the input PDF or DOCX file",
    )
    parser.add_argument(
        "--template", "-t",
        choices=list(TEMPLATE_REGISTRY.keys()),
        default=DEFAULT_TEMPLATE,
        help=f"Output format (default: {DEFAULT_TEMPLATE})",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output directory (default: output/)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        default=False,
        help="Disable formula OCR entirely — fast mode, formulas render as images",
    )
    parser.add_argument(
        "--ocr-budget",
        type=float,
        default=90.0,
        metavar="SECONDS",
        help="Max seconds to spend on formula OCR (default: 90). Use 0 for --no-ocr.",
    )

    args = parser.parse_args()

    # Configure OCR budget before extraction runs
    from extractor.pdf_extractor import set_ocr_budget
    if args.no_ocr:
        set_ocr_budget(0)
        print("[main] OCR disabled (--no-ocr)", file=sys.stderr, flush=True)
    else:
        set_ocr_budget(args.ocr_budget)
        if args.ocr_budget < 900:
            print(f"[main] OCR budget: {args.ocr_budget:.0f}s", file=sys.stderr, flush=True)

    # Safety print to stderr (not buffered like log handlers)
    print(f"[main] Starting: {args.input} → {args.template}", file=sys.stderr, flush=True)

    try:
        pdf = run(
            input_file=args.input,
            template=args.template,
            output_dir=args.output,
        )
        print(f"Output: {pdf}")
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  Error: {e}\n", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n  Pipeline failed: {e}\n", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"\n  Unexpected error: {type(e).__name__}: {e}\n", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(3)


if __name__ == "__main__":
    main()
