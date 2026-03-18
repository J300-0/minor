"""
core/config.py  —  All project-wide paths and constants.
Change things here; everything else updates automatically.
"""

import os

ROOT             = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR        = os.path.join(ROOT, "input")
INTERMEDIATE_DIR = os.path.join(ROOT, "intermediate")
OUTPUT_DIR       = os.path.join(ROOT, "output")
TEMPLATES_DIR    = os.path.join(ROOT, "templates")


# ── Intermediate files ────────────────────────────────────────────────────────
EXTRACTED_TXT   = os.path.join(INTERMEDIATE_DIR, "extracted.txt")
EXTRACTED_RICH  = os.path.join(INTERMEDIATE_DIR, "extracted_rich.json")
STRUCTURED_JSON = os.path.join(INTERMEDIATE_DIR, "structured.json")
GENERATED_TEX   = os.path.join(INTERMEDIATE_DIR, "generated.tex")

# ── Template registry ─────────────────────────────────────────────────────────
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
# Match this EXACTLY to the model name shown in LM Studio's server tab
LM_STUDIO_URL     = os.getenv("LM_STUDIO_URL",   "http://localhost:1234/v1")
LM_STUDIO_MODEL   = os.getenv("LM_STUDIO_MODEL", "qwen3-8b")
LM_STUDIO_ENABLED = True

# Timeout for the CONNECT + first-byte only (streaming keeps socket alive).
# 60s is plenty — if the model hasn't started responding in 60s it's hung.
LM_STUDIO_TIMEOUT = 60

# Batch size: chars of paper text per LLM call.
# 10K chars ≈ 2500 tokens — good balance of progress visibility vs call count.
# Increase toward 50K-100K once timeouts are resolved.
LM_BATCH_CHARS  = 10_000

# Max output tokens per response. 8192 covers any section+refs JSON.
# The model stops naturally when JSON is complete, well before this limit.
LM_MAX_TOKENS   = 8_192