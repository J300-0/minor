"""
check_structure.py  —  Validate project structure and imports before running

Usage: python check_structure.py

Checks:
  1. All required files exist
  2. All imports resolve without error
  3. No conflicting old stages/ folder
  4. Filename typo (heurestic vs heuristic)
  5. Template mappers all have render()

Writes findings to logs/structure_check.log
"""

import os, sys, importlib.util, traceback, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.logger import get_logger, LOGS_DIR

# Use a separate log file so it doesn't pollute pipeline.log
log_path = os.path.join(LOGS_DIR, "structure_check.log")
os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(
    filename=log_path,
    filemode="w",
    level=logging.DEBUG,
    format="%(levelname)-8s  %(message)s",
)
log = logging.getLogger("structure_check")

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"

results = []


def check(label, fn):
    try:
        msg = fn()
        results.append((True, label, msg or ""))
        log.info(f"PASS  {label}  {msg or ''}")
    except Exception as e:
        results.append((False, label, str(e)))
        log.error(f"FAIL  {label}  {e}\n{traceback.format_exc()}")


# ── 1. Required files ─────────────────────────────────────────────────────────

REQUIRED = [
    "main.py",
    "core/__init__.py", "core/config.py", "core/models.py",
    "core/pipeline.py", "core/logger.py",
    "extractor/pdf_extractor.py", "extractor/docx_extractor.py",
    "ai/heuristic_parser.py", "ai/structure_llm.py", "ai/cleaning_llm.py",
    "mapper/base_mapper.py",
    "compiler/latex_compiler.py",
    "templates/ieee/mapper.py",   "templates/ieee/template.tex.j2",
    "templates/acm/mapper.py",    "templates/acm/template.tex.j2",
    "templates/springer/mapper.py","templates/springer/template.tex.j2",
    "templates/elsevier/mapper.py","templates/elsevier/template.tex.j2",
    "templates/apa/mapper.py",    "templates/apa/template.tex.j2",
    "templates/arxiv/mapper.py",  "templates/arxiv/template.tex.j2",
    "requirements.txt",
    "schema/paper_schema.json",
]

for rel in REQUIRED:
    path = rel
    check(f"file exists: {rel}", lambda p=path: (
        None if os.path.exists(p)
        else (_ for _ in ()).throw(FileNotFoundError(f"Missing: {p}"))
    ))

# ── 2. Old stages/ folder (should be deleted) ────────────────────────────────

def check_no_stages():
    if os.path.isdir("stages"):
        raise RuntimeError(
            "Old stages/ folder still exists — it will shadow new ai/ imports.\n"
            "  Action: delete the entire stages/ folder.\n"
            "  The new equivalents are:\n"
            "    stages/layout_parser.py        → extractor/pdf_extractor.py\n"
            "    stages/document_parser.py      → ai/heuristic_parser.py\n"
            "    stages/ai_structure_detector.py→ ai/structure_llm.py\n"
            "    stages/normalizer.py           → ai/cleaning_llm.py\n"
            "    stages/template_renderer.py    → templates/*/mapper.py\n"
            "    stages/latex_compiler.py       → compiler/latex_compiler.py"
        )
    return "stages/ not present (good)"

check("no old stages/ folder", check_no_stages)

# ── 3. Filename typo check ────────────────────────────────────────────────────

def check_typo():
    typo = "ai/heurestic_parser.py"
    correct = "ai/heuristic_parser.py"
    if os.path.exists(typo) and not os.path.exists(correct):
        raise RuntimeError(
            f"Filename typo: '{typo}' exists but pipeline imports '{correct}'.\n"
            f"  Action: rename heurestic_parser.py → heuristic_parser.py"
        )
    if os.path.exists(typo) and os.path.exists(correct):
        return f"WARNING: both '{typo}' and '{correct}' exist — delete the typo file"
    return "ai/heuristic_parser.py (correct spelling)"

check("heuristic_parser.py filename", check_typo)

# ── 4. Import checks ──────────────────────────────────────────────────────────

IMPORTS = [
    ("core.models",        "Document, Author, Section, Table, Reference"),
    ("core.config",        "TEMPLATE_REGISTRY, DEFAULT_TEMPLATE"),
    ("core.logger",        "get_logger"),
    ("core.pipeline",      "run"),
    ("extractor.pdf_extractor",  "extract"),
    ("extractor.docx_extractor", "extract"),
    ("ai.heuristic_parser", "parse, extract_references"),
    ("ai.structure_llm",   "parse"),
    ("ai.cleaning_llm",    "normalize"),
    ("mapper.base_mapper", "latex_escape, inject_tables"),
    ("compiler.latex_compiler", "compile"),
]

for mod, symbols in IMPORTS:
    def _try(m=mod, s=symbols):
        module = importlib.import_module(m)
        for sym in [x.strip() for x in s.split(",")]:
            if not hasattr(module, sym):
                raise AttributeError(f"'{sym}' not found in {m}")
        return f"{s}"
    check(f"import {mod}", _try)

# ── 5. Template mappers all have render() ────────────────────────────────────

from core.pipeline import _load_mapper
for tmpl in ["ieee", "acm", "springer", "elsevier", "apa", "arxiv"]:
    def _check_mapper(t=tmpl):
        m = _load_mapper(t)
        if not hasattr(m, "render"):
            raise AttributeError(f"templates/{t}/mapper.py missing render()")
        return "render() present"
    check(f"templates/{tmpl}/mapper.render()", _check_mapper)

# ── 6. IEEEtran.cls present ──────────────────────────────────────────────────

check("IEEEtran.cls", lambda: (
    None if os.path.exists("templates/ieee/IEEEtran.cls")
    else (_ for _ in ()).throw(FileNotFoundError(
        "templates/ieee/IEEEtran.cls missing — IEEE output will fail.\n"
        "  Download from: https://www.ctan.org/pkg/ieeetran"
    ))
))

# ── 7. pdflatex available ────────────────────────────────────────────────────

import shutil
check("pdflatex on PATH", lambda: (
    f"found at {shutil.which('pdflatex')}" if shutil.which("pdflatex")
    else (_ for _ in ()).throw(EnvironmentError(
        "pdflatex not found on PATH.\n"
        "  Windows: https://miktex.org/\n"
        "  Linux:   sudo apt install texlive-full\n"
        "  macOS:   https://tug.org/mactex/"
    ))
))

# ── Print results ─────────────────────────────────────────────────────────────

passed = sum(1 for ok, _, _ in results if ok)
failed = sum(1 for ok, _, _ in results if not ok)

print(f"\n{'─' * 60}")
print(f"  Structure check  —  {passed} passed, {failed} failed")
print(f"{'─' * 60}")

for ok, label, msg in results:
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}")
    if not ok:
        # Indent the error message
        for line in msg.split("\n"):
            print(f"          {line}")

print(f"\n  Log written to: {log_path}")

if failed:
    print(f"\n  ❌  Fix the {failed} issue(s) above before running main.py\n")
    sys.exit(1)
else:
    print(f"\n  ✅  All checks passed — ready to run:\n"
          f"      python main.py input/your_paper.pdf\n")