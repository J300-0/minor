"""
core/config.py
All project-wide paths and constants live here.
Change paths in one place, everything else updates.
"""

import os

# ── Root ─────────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Directories ──────────────────────────────────────────────────────────────
INPUT_DIR        = os.path.join(ROOT, "input")
INTERMEDIATE_DIR = os.path.join(ROOT, "intermediate")
OUTPUT_DIR       = os.path.join(ROOT, "output")
TEMPLATE_DIR     = os.path.join(ROOT, "templates", "ieee")

# ── Intermediate file paths ───────────────────────────────────────────────────
EXTRACTED_TXT    = os.path.join(INTERMEDIATE_DIR, "extracted.txt")
STRUCTURED_JSON  = os.path.join(INTERMEDIATE_DIR, "structured.json")
GENERATED_TEX    = os.path.join(INTERMEDIATE_DIR, "generated.tex")

# ── Template ──────────────────────────────────────────────────────────────────
TEMPLATE_NAME    = "template.tex.j2"

# ── Supported input extensions ────────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}

# ── pdflatex ──────────────────────────────────────────────────────────────────
PDFLATEX_PASSES = 2          # run twice to resolve cross-references
PDFLATEX_FLAGS  = ["-interaction=nonstopmode"]