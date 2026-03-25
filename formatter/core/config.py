"""
core/config.py — Paths, constants, template registry.
"""
import os

# Project root = parent of core/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories
INPUT_DIR = os.path.join(PROJECT_ROOT, "input")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
INTERMEDIATE_DIR = os.path.join(PROJECT_ROOT, "intermediate")
TEMPLATE_DIR = os.path.join(PROJECT_ROOT, "template")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# Ensure dirs exist
for d in [OUTPUT_DIR, INTERMEDIATE_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# Template registry: name -> (cls file or None, template.tex.j2)
DEFAULT_TEMPLATE = "ieee"

TEMPLATE_REGISTRY = {
    "ieee":     {"cls": "IEEEtran.cls",     "j2": "template.tex.j2"},
    "acm":      {"cls": "acmart.cls",        "j2": "template.tex.j2"},
    "springer": {"cls": "llncs.cls",         "j2": "template.tex.j2"},
    "elsevier": {"cls": "elsarticle.cls",    "j2": "template.tex.j2"},
    "apa":      {"cls": None,                "j2": "template.tex.j2"},
    "arxiv":    {"cls": None,                "j2": "template.tex.j2"},
}
