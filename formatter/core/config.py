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

# ── LM Studio (local LLM) ─────────────────────────────────────────────────────
# Start LM Studio → Local Server → Load your model → Start Server
# Default port is 1234. Change if you've configured a different port.
LM_STUDIO_URL        = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL      = os.getenv("LM_STUDIO_MODEL", "qwen3-8b")   # model name as shown in LM Studio
LM_STUDIO_TIMEOUT    = 120          # seconds per request
LM_STUDIO_ENABLED    = True         # set False to always use heuristic parser

# Batching: large documents are split into chunks before sending to the LLM
# Each chunk is sent separately; results are merged into one Document.
LM_BATCH_CHARS       = 6000         # characters per batch (~1500-2000 tokens)
LM_MAX_TOKENS        = 4096         # max tokens for LLM response per batch