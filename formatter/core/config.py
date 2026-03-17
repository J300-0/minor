"""
core/config.py  —  All project-wide paths and constants.

Change things here; everything else updates automatically.
"""

import os

ROOT             = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR        = os.path.join(ROOT, "input")
INTERMEDIATE_DIR = os.path.join(ROOT, "intermediate")
OUTPUT_DIR       = os.path.join(ROOT, "output")
TEMPLATES_DIR    = os.path.join(ROOT, "templates")   # parent of ieee/, acm/, etc.

# ── Intermediate files ────────────────────────────────────────────────────────
EXTRACTED_TXT   = os.path.join(INTERMEDIATE_DIR, "extracted.txt")
EXTRACTED_RICH  = os.path.join(INTERMEDIATE_DIR, "extracted_rich.json")
STRUCTURED_JSON = os.path.join(INTERMEDIATE_DIR, "structured.json")
GENERATED_TEX   = os.path.join(INTERMEDIATE_DIR, "generated.tex")

# ── Template registry  ────────────────────────────────────────────────────────
# Each key is the --template CLI arg; value is the subfolder under templates/
TEMPLATE_REGISTRY = {
    "ieee":     "ieee",
    "acm":      "acm",
    "springer": "springer",
    "elsevier": "elsevier",
    "apa":      "apa",
    "arxiv":    "arxiv",
}
DEFAULT_TEMPLATE  = "ieee"
TEMPLATE_FILENAME = "template.tex.j2"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}

# ── pdflatex ──────────────────────────────────────────────────────────────────
PDFLATEX_PASSES = 2
PDFLATEX_FLAGS  = ["-interaction=nonstopmode"]

# ── LM Studio ─────────────────────────────────────────────────────────────────
LM_STUDIO_URL     = os.getenv("LM_STUDIO_URL",   "http://localhost:1234/v1")
LM_STUDIO_MODEL   = os.getenv("LM_STUDIO_MODEL", "qwen3-9b")
LM_STUDIO_TIMEOUT = 120
LM_STUDIO_ENABLED = True
LM_BATCH_CHARS    = 6000
LM_MAX_TOKENS     = 61440