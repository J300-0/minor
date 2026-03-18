"""
core/logger.py  —  Centralised logging for the formatter pipeline

Writes to:
  - logs/pipeline.log        — full DEBUG log, rotating 5MB × 3
  - logs/pipeline_latest.log — overwritten each run (easy to paste for debugging)

Usage:
    from core.logger import get_logger
    log = get_logger(__name__)
    log.info("done")
    log.warning("no tables found")
    log.error("pdflatex failed", exc_info=True)
"""

import logging
import logging.handlers
import os, sys, traceback

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT, "logs")
LOG_FILE         = os.path.join(LOGS_DIR, "pipeline.log")
LOG_FILE_LATEST  = os.path.join(LOGS_DIR, "pipeline_latest.log")

_FILE_FMT    = "%(asctime)s  %(levelname)-8s  %(name)-28s  %(message)s"
_CONSOLE_FMT = "%(levelname)-8s  %(message)s"
_DATE_FMT    = "%Y-%m-%d %H:%M:%S"

_initialised = False


def _init():
    global _initialised
    if _initialised:
        return
    _initialised = True

    os.makedirs(LOGS_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Rotating log — keeps last 3 × 5MB
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(fh)

    # Latest-run log — always overwritten, easy to paste for bug reports
    lh = logging.FileHandler(LOG_FILE_LATEST, mode="w", encoding="utf-8")
    lh.setLevel(logging.DEBUG)
    lh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(lh)

    # Silence third-party DEBUG spam that bloats the log to 180MB
    for noisy in ("pdfminer", "pdfplumber", "PIL", "urllib3", "httpx",
                  "httpcore", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Console — WARNING and above only (errors surface without log spam)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    _init()
    return logging.getLogger(name)


# ── Pipeline-level convenience functions ──────────────────────────────────────

_pl = None

def _pipeline() -> logging.Logger:
    global _pl
    if _pl is None:
        _pl = get_logger("pipeline")
    return _pl


def log_run_start(input_file: str, template: str, use_ai: bool):
    from core.config import LM_STUDIO_MODEL, LM_BATCH_CHARS, LM_MAX_TOKENS, LM_STUDIO_TIMEOUT
    _pipeline().info("=" * 72)
    _pipeline().info(f"RUN START")
    _pipeline().info(f"  input     = {os.path.basename(input_file)}")
    _pipeline().info(f"  template  = {template}")
    _pipeline().info(f"  use_ai    = {use_ai}")
    _pipeline().info(f"  model     = {LM_STUDIO_MODEL}")
    _pipeline().info(f"  batch     = {LM_BATCH_CHARS} chars")
    _pipeline().info(f"  max_tok   = {LM_MAX_TOKENS}")
    _pipeline().info(f"  timeout   = {LM_STUDIO_TIMEOUT}s (connect+first-token)")
    _pipeline().info("=" * 72)


def log_stage(n: int, name: str, desc: str):
    _pipeline().info(f"[{n}/5] {name} — {desc}")


def log_doc_stats(doc):
    n_tables = sum(len(s.tables) for s in doc.sections)
    _pipeline().info(
        f"  doc: title={repr(doc.title[:60])}  "
        f"authors={len(doc.authors)}  sections={len(doc.sections)}  "
        f"tables={n_tables}  refs={len(doc.references)}"
    )


def log_extraction(chars: int, tables: int, images: int):
    _pipeline().info(f"  extracted: {chars} chars | {tables} tables | {images} images")
    if chars < 500:
        _pipeline().warning(
            f"Very short extraction ({chars} chars) — "
            "pdfplumber may have failed; check intermediate/extracted.txt"
        )
    if tables == 0:
        _pipeline().warning(
            "No tables extracted — paper may use image-based tables, "
            "or pdfplumber line-strategy found no ruled lines"
        )


def log_refs(count: int, source: str):
    _pipeline().info(f"  references: {count} found via {source}")
    if count == 0:
        _pipeline().warning(
            "No references found — check intermediate/extracted.txt for [1] markers"
        )
    elif count == 1:
        _pipeline().warning(
            "Only 1 reference found — all refs may have merged into one blob; "
            "check extract_references() splitter in ai/heuristic_parser.py"
        )


def log_error(stage: str, exc: Exception, fatal: bool = False):
    level = logging.CRITICAL if fatal else logging.ERROR
    _pipeline().log(level, f"[{stage}] {'FATAL ' if fatal else ''}error: {exc}")
    _pipeline().log(level, traceback.format_exc())


def log_run_end(pdf_path: str, elapsed: float):
    _pipeline().info(f"RUN END — {pdf_path}  ({elapsed:.1f}s)")
    _pipeline().info("")
    _pipeline().info(
        f"  ↳ To share this log for debugging: paste contents of "
        f"logs/pipeline_latest.log"
    )
    _pipeline().info("")