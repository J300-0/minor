"""
compiler/latex_compiler.py — pdflatex 2-pass compilation.

Flags: -interaction=nonstopmode -halt-on-error
Outputs: generated_{template}.pdf
"""
import os
import shutil
import subprocess
import logging

from core.config import INTERMEDIATE_DIR

log = logging.getLogger("paper_formatter")


def compile_latex(tex_path: str, output_dir: str, template_name: str) -> str:
    """
    Compile a .tex file to PDF using pdflatex (2 passes for cross-references).
    Returns path to the output PDF.
    """
    # Check pdflatex is available
    if not shutil.which("pdflatex"):
        raise RuntimeError(
            "pdflatex not found on PATH. Install TeX Live or MiKTeX.\n"
            "  Ubuntu/Debian: sudo apt install texlive-latex-extra texlive-fonts-recommended\n"
            "  macOS: brew install --cask mactex"
        )

    tex_dir = os.path.dirname(os.path.abspath(tex_path))
    tex_name = os.path.basename(tex_path)
    tex_stem = os.path.splitext(tex_name)[0]

    # Clean stale auxiliary files that may contain null bytes from previous runs.
    # Corrupted .aux files cause "Text line contains an invalid character" errors.
    for ext in (".aux", ".out", ".toc", ".lof", ".lot", ".log"):
        stale = os.path.join(tex_dir, tex_stem + ext)
        if os.path.isfile(stale):
            try:
                # Check for null bytes — sign of corruption
                with open(stale, "rb") as f:
                    data = f.read()
                if b"\x00" in data:
                    log.debug("  Removing corrupted %s (null bytes)", ext)
                    os.remove(stale)
            except Exception:
                pass

    cmd = [
        "pdflatex",
        "-interaction=nonstopmode",
        f"-output-directory={tex_dir}",
        tex_name,
    ]

    # Run 2 passes (second pass resolves cross-references)
    log_path = os.path.join(INTERMEDIATE_DIR, "generated.log")
    for pass_num in (1, 2):
        log.info("  pdflatex pass %d/2...", pass_num)
        result = subprocess.run(
            cmd, cwd=tex_dir,
            capture_output=True, timeout=120,
        )
        # Decode stdout/stderr with error handling (pdflatex may emit non-UTF-8)
        stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""

        # Save log — append pass 2 so both passes are preserved
        if stdout:
            mode = "w" if pass_num == 1 else "a"
            with open(log_path, mode, encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n  pdflatex pass {pass_num}\n{'='*60}\n")
                f.write(stdout)

        if result.returncode != 0:
            # Extract error from output
            error_lines = []
            for line in stdout.split("\n"):
                if line.startswith("!") or "Fatal error" in line:
                    error_lines.append(line)

            error_msg = "\n".join(error_lines[:5]) if error_lines else ""

            # Check if "no output PDF file produced" (fatal)
            fatal = "no output PDF file produced" in stdout

            if fatal:
                log.error("  pdflatex fatal failure (pass %d):\n%s", pass_num, error_msg)
                raise RuntimeError(
                    f"pdflatex compilation failed (pass {pass_num}):\n{error_msg}\n"
                    f"See {log_path} for full output."
                )
            elif error_lines:
                log.warning("  pdflatex pass %d had errors (non-fatal):\n%s",
                            pass_num, error_msg)
            # Continue — nonstopmode may still produce output despite errors

    # Move PDF to output directory
    pdf_name = os.path.splitext(tex_name)[0] + ".pdf"
    src_pdf = os.path.join(tex_dir, pdf_name)
    dst_pdf = os.path.join(output_dir, f"generated_{template_name}.pdf")

    if os.path.isfile(src_pdf):
        os.makedirs(output_dir, exist_ok=True)
        shutil.copy2(src_pdf, dst_pdf)
        log.info("  Output: %s", dst_pdf)
        return dst_pdf
    else:
        raise RuntimeError(f"PDF not generated. Expected: {src_pdf}")
