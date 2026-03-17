"""
main.py  —  CLI entry point for ai-paper-formatter

Usage:
    python main.py input/paper.pdf
    python main.py input/paper.pdf --template acm
    python main.py input/paper.pdf --template springer --no-ai
    python main.py input/paper.docx --template ieee

Available templates: ieee, acm, springer, elsevier, apa, arxiv
"""

import sys, os, argparse

# Make sure project root is on the path regardless of where you run from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.pipeline import run
from core.config   import TEMPLATE_REGISTRY, DEFAULT_TEMPLATE


def main():
    parser = argparse.ArgumentParser(
        description="Format academic papers into IEEE/ACM/Springer/Elsevier/APA/arXiv PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="Path to input PDF or DOCX")
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
        "--no-ai",
        action="store_true",
        help="Skip LM Studio; use heuristic parser only",
    )

    args = parser.parse_args()

    try:
        pdf = run(
            input_file  = args.input,
            template    = args.template,
            output_dir  = args.output,
            use_ai      = not args.no_ai,
        )
        print(f"Output: {pdf}")
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌  {e}\n", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n  ❌  Pipeline failed: {e}\n", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()