"""compiler/latex_compiler.py  —  .tex → PDF via pdflatex"""
import os, shutil, subprocess
from core.config import PDFLATEX_PASSES, PDFLATEX_FLAGS


def compile(tex_file: str, output_dir: str) -> str:
    if not shutil.which("pdflatex"):
        raise EnvironmentError(
            "pdflatex not found.\n"
            "  Windows: https://miktex.org/\n"
            "  Linux:   sudo apt install texlive-full\n"
            "  macOS:   https://tug.org/mactex/"
        )
    os.makedirs(output_dir, exist_ok=True)
    cmd = ["pdflatex"] + PDFLATEX_FLAGS + ["-output-directory", output_dir, tex_file]

    for n in range(1, PDFLATEX_PASSES + 1):
        print(f"         pdflatex pass {n}/{PDFLATEX_PASSES}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and n == 1:
            _report(result.stdout)
            raise RuntimeError("pdflatex failed — see log above")

    pdf = os.path.join(output_dir, os.path.splitext(os.path.basename(tex_file))[0] + ".pdf")
    if not os.path.exists(pdf):
        raise FileNotFoundError(f"pdflatex ran but PDF not found: {pdf}")
    print(f"         → {pdf}")
    return pdf


def _report(log: str):
    lines = log.split("\n")
    print("\n  ── pdflatex error (last 40 lines) ───")
    print("\n".join(lines[-40:]))
    print("  ─────────────────────────────────────\n")