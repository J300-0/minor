"""
core/logger.py — Centralised logging for the formatter pipeline.

Writes to:
  logs/pipeline.log        — rotating 5 MB × 3, full DEBUG
  logs/pipeline_latest.log — overwritten each run
"""
import logging
import logging.handlers
import os, sys, traceback

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT, "logs")
_LOG_FILE    = os.path.join(LOGS_DIR, "pipeline.log")
_LOG_LATEST  = os.path.join(LOGS_DIR, "pipeline_latest.log")
_FILE_FMT    = "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s"
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

    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(fh)

    lh = logging.FileHandler(_LOG_LATEST, mode="w", encoding="utf-8")
    lh.setLevel(logging.DEBUG)
    lh.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(lh)

    # Silence noisy third-party libs
    for lib in ("pdfminer", "pdfplumber", "PIL", "urllib3", "httpx", "charset_normalizer"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    _init()
    return logging.getLogger(name)


# ── Pipeline helpers ──────────────────────────────────────────────────────────

_pl = None

def _pl_log() -> logging.Logger:
    global _pl
    if _pl is None:
        _pl = get_logger("pipeline")
    return _pl


def log_run_start(input_file: str, template: str):
    _pl_log().info("=" * 70)
    _pl_log().info("RUN START")
    _pl_log().info(f"  input    = {os.path.basename(input_file)}")
    _pl_log().info(f"  template = {template}")
    _pl_log().info(f"  parser   = heuristic")
    _pl_log().info("=" * 70)


def log_stage(n: int, name: str, desc: str):
    _pl_log().info(f"[{n}/5] {name} — {desc}")


def log_extraction(chars: int, tables: int, images: int):
    _pl_log().info(f"  extracted: {chars} chars | {tables} tables | {images} images")
    if chars < 200:
        _pl_log().warning("Very short extraction — check intermediate/extracted.txt")


def log_doc_stats(doc):
    n_tables = sum(len(s.tables) for s in doc.sections)
    _pl_log().info(
        f"  doc: title={repr(doc.title[:50])}  "
        f"authors={len(doc.authors)}  sections={len(doc.sections)}  "
        f"tables={n_tables}  refs={len(doc.references)}"
    )


def log_error(stage: str, exc: Exception, fatal: bool = False):
    level = logging.CRITICAL if fatal else logging.ERROR
    _pl_log().log(level, f"[{stage}] {'FATAL ' if fatal else ''}error: {exc}")
    _pl_log().log(level, traceback.format_exc())


def log_run_end(pdf_path: str, elapsed: float):
    _pl_log().info(f"RUN END — {pdf_path}  ({elapsed:.1f}s)")
    _pl_log().info("")
