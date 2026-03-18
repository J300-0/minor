"""
compiler/latex_compiler.py — Stage 5: .tex → PDF via pdflatex.
Runs pdflatex twice (needed for cross-references / table of contents).
"""
import os, shutil, subprocess
from core.config import PDFLATEX_PASSES, PDFLATEX_FLAGS, CLS_FILES, TEMPLATES_DIR
from core.logger import get_logger

log = get_logger(__name__)


def compile(tex_path: str, output_dir: str, template_name: str) -> str:
    """
    Compile tex_path to PDF inside a temp work dir, then copy to output_dir.
    Returns path to the final PDF.
    """
    if not shutil.which("pdflatex"):
        raise RuntimeError(
            "pdflatex not found on PATH.\n"
            "  Windows: https://miktex.org/\n"
            "  Linux:   sudo apt install texlive-full\n"
            "  macOS:   https://tug.org/mactex/"
        )

    os.makedirs(output_dir, exist_ok=True)
    work_dir = os.path.dirname(tex_path)    # intermediate/ — compile in place
    tex_name = os.path.basename(tex_path)

    # Copy required .cls file into the work dir if needed
    cls = CLS_FILES.get(template_name)
    if cls:
        src = os.path.join(TEMPLATES_DIR, template_name, cls)
        dst = os.path.join(work_dir, cls)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            log.info("Copied %s to work dir", cls)

    cmd = ["pdflatex"] + PDFLATEX_FLAGS + [tex_name]

    for n in range(1, PDFLATEX_PASSES + 1):
        print(f"         pdflatex pass {n}/{PDFLATEX_PASSES}...")
        result = subprocess.run(
            cmd, cwd=work_dir,
            capture_output=True, text=True,
        )
        log.debug("pdflatex pass %d stdout (last 1500 chars):\n%s",
                  n, result.stdout[-1500:] if result.stdout else "")
        if result.returncode != 0:
            _log_pdflatex_error(result.stdout)
            raise RuntimeError(
                f"pdflatex failed on pass {n} — check logs/pipeline_latest.log\n"
                f"Also inspect: {os.path.join(work_dir, tex_name.replace('.tex', '.log'))}"
            )

    # Move final PDF to output_dir with template name in filename
    pdf_name  = tex_name.replace(".tex", ".pdf")
    src_pdf   = os.path.join(work_dir, pdf_name)
    if not os.path.exists(src_pdf):
        raise RuntimeError(f"pdflatex finished but PDF not found: {src_pdf}")

    # Rename: generated.pdf → generated_ieee.pdf (includes format type)
    base, ext = os.path.splitext(pdf_name)
    dest_name = f"{base}_{template_name}{ext}"
    dest_pdf  = os.path.join(output_dir, dest_name)
    shutil.move(src_pdf, dest_pdf)
    log.info("PDF written to %s", dest_pdf)
    return dest_pdf


def _log_pdflatex_error(stdout: str):
    """Extract and log the most useful error lines from pdflatex output."""
    error_lines = [l for l in (stdout or "").splitlines()
                   if l.startswith("!") or "Error" in l or "error" in l]
    if error_lines:
        log.error("pdflatex errors:\n%s", "\n".join(error_lines[:20]))
    else:
        log.error("pdflatex failed — no specific error found in stdout")
