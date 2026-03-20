"""
core/logger.py — Centralised logging for the formatter pipeline.

Writes to:
  logs/pipeline.log        — rotating 5 MB x 3, full DEBUG
  logs/pipeline_latest.log — overwritten each run
"""
import logging
import logging.handlers
import os
import sys
import traceback

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT, "logs")
_LOG_FILE    = os.path.join(LOGS_DIR, "pipeline.log")
_LOG_LATEST  = os.path.join(LOGS_DIR, "pipeline_latest.log")
_FILE_FMT    = "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s"
_DATE_FMT    = "%Y-%m-%d %H:%M:%S"

_initialised = False


def _init():
    """Set up root logger with rotating file, latest file, and stderr handlers."""
    global _initialised
    if _initialised:
        return
    _initialised = True
    os.makedirs(LOGS_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Rotating file handler  (full debug)
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(fh)

    # Latest-run file handler  (overwritten each run)
    lh = logging.FileHandler(_LOG_LATEST, mode="w", encoding="utf-8")
    lh.setLevel(logging.DEBUG)
    lh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(lh)

    # Silence noisy third-party loggers  (CRITICAL: pdfminer floods root)
    for lib in ("pdfminer", "pdfplumber", "PIL", "urllib3", "httpx",
                "charset_normalizer"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    # Console handler  (WARNING+)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Auto-initialises on first call."""
    _init()
    return logging.getLogger(name)


# ── Pipeline convenience helpers ─────────────────────────────────────────────

_pl: logging.Logger = None


def _pl_log() -> logging.Logger:
    global _pl
    if _pl is None:
        _pl = get_logger("pipeline")
    return _pl


def log_run_start(input_file: str, template: str):
    _pl_log().info("=" * 70)
    _pl_log().info("RUN START")
    _pl_log().info("  input    = %s", os.path.basename(input_file))
    _pl_log().info("  template = %s", template)
    _pl_log().info("  parser   = heuristic")
    _pl_log().info("=" * 70)


def log_stage(n, name: str, desc: str):
    _pl_log().info("[%s] %s — %s", n, name, desc)


def log_extraction(chars: int, tables: int, images: int):
    _pl_log().info("  extracted: %d chars | %d tables | %d images", chars, tables, images)
    if chars < 200:
        _pl_log().warning("Very short extraction — check intermediate/extracted.txt")


def log_doc_stats(doc):
    n_tables = sum(len(s.tables) for s in doc.sections)
    _pl_log().info(
        "  doc: title=%r  authors=%d  sections=%d  tables=%d  refs=%d",
        (doc.title or "")[:50], len(doc.authors), len(doc.sections),
        n_tables, len(doc.references),
    )


def log_error(stage: str, exc: Exception, fatal: bool = False):
    level = logging.CRITICAL if fatal else logging.ERROR
    _pl_log().log(level, "[%s] %serror: %s", stage, "FATAL " if fatal else "", exc)
    _pl_log().log(level, traceback.format_exc())


def log_run_end(pdf_path: str, elapsed: float):
    _pl_log().info("RUN END — %s  (%.1fs)", pdf_path, elapsed)
    _pl_log().info("")
