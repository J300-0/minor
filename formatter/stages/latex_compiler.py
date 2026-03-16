"""
stages/latex_compiler.py
Stage 5 — LaTeX Compiler: .tex → formatted PDF

Responsibility:
  - Check pdflatex is available, give helpful install message if not
  - Run pdflatex (twice, to resolve cross-references)
  - Surface errors clearly (last N lines of log) instead of silently failing
  - Return path to the output PDF
"""

import os
import shutil
import subprocess
from core.config import PDFLATEX_PASSES, PDFLATEX_FLAGS


def compile(tex_file: str, output_dir: str) -> str:
    _check_pdflatex()
    os.makedirs(output_dir, exist_ok=True)

    cmd = ["pdflatex"] + PDFLATEX_FLAGS + ["-output-directory", output_dir, tex_file]

    for pass_n in range(1, PDFLATEX_PASSES + 1):
        print(f"         pdflatex pass {pass_n}/{PDFLATEX_PASSES}...")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0 and pass_n == 1:
            _report_error(result.stdout)
            raise RuntimeError("pdflatex failed — see log above")

    pdf_path = _expected_pdf(tex_file, output_dir)
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"pdflatex ran but PDF not found: {pdf_path}")

    print(f"         → {pdf_path}")
    return pdf_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_pdflatex():
    if not shutil.which("pdflatex"):
        raise EnvironmentError(
            "pdflatex not found on PATH.\n"
            "  Linux : sudo apt install texlive-full\n"
            "  macOS : install MacTeX from https://tug.org/mactex/\n"
            "  Windows: install MiKTeX from https://miktex.org/"
        )

def _expected_pdf(tex_file: str, output_dir: str) -> str:
    base = os.path.splitext(os.path.basename(tex_file))[0]
    return os.path.join(output_dir, base + ".pdf")

def _report_error(log: str):
    lines = log.split("\n")
    print("\n  ── pdflatex error (last 40 lines) ──────────────────")
    print("\n".join(lines[-40:]))
    print("  ────────────────────────────────────────────────────\n")