"""
core/config.py — All project-wide paths and constants.
"""
import os

ROOT             = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR        = os.path.join(ROOT, "input")
INTERMEDIATE_DIR = os.path.join(ROOT, "intermediate")
OUTPUT_DIR       = os.path.join(ROOT, "output")
TEMPLATES_DIR    = os.path.join(ROOT, "template")
LOGS_DIR         = os.path.join(ROOT, "logs")

# Intermediate file paths
EXTRACTED_TXT   = os.path.join(INTERMEDIATE_DIR, "extracted.txt")
STRUCTURED_JSON = os.path.join(INTERMEDIATE_DIR, "structured.json")
GENERATED_TEX   = os.path.join(INTERMEDIATE_DIR, "generated.tex")

# Templates
TEMPLATE_REGISTRY = {
    "ieee":     "ieee",
    "acm":      "acm",
    "springer": "springer",
    "elsevier": "elsevier",
    "apa":      "apa",
    "arxiv":    "arxiv",
}
DEFAULT_TEMPLATE     = "ieee"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}

# pdflatex
PDFLATEX_PASSES = 2
PDFLATEX_FLAGS  = ["-interaction=nonstopmode", "-halt-on-error"]

# .cls files that must sit next to the .tex during compilation
CLS_FILES = {
    "ieee":     "IEEEtran.cls",
    "springer": "llncs.cls",
    "acm":      "acmart.cls",
    "elsevier": "elsarticle.cls",
}
