"""
stages/ai_structure_detector.py
Stage 2 (AI) — Raw text → Structured Document via LM Studio (Qwen)

How it works:
  1. Split extracted text into batches (~6000 chars each)
  2. Send each batch to LM Studio with a structured extraction prompt
  3. Parse JSON responses and merge into a single Document
  4. Fall back to heuristic document_parser if LM Studio is unavailable

The LLM handles documents that don't follow standard academic structure —
creative layouts, non-standard section names, mixed languages, reports,
theses, etc. — things the heuristic parser struggles with.

Setup:
  1. Open LM Studio → Local Server tab
  2. Load your Qwen model
  3. Click "Start Server" (default: http://localhost:1234)
  4. Run the pipeline normally — it auto-detects LM Studio
"""

import json
import re
import urllib.request
import urllib.error
from core.models  import Document, Section, Author
from core.config  import (LM_STUDIO_URL, LM_STUDIO_MODEL, LM_STUDIO_TIMEOUT,
                           LM_STUDIO_ENABLED, LM_BATCH_CHARS, LM_MAX_TOKENS)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse(extracted_path: str) -> Document | None:
    """
    Try to parse the extracted text with LM Studio.
    Returns a Document on success, None if LM Studio is unavailable
    (pipeline will fall back to heuristic document_parser).
    """
    if not LM_STUDIO_ENABLED:
        return None

    if not _lm_studio_running():
        print("         [AI] LM Studio not running — using heuristic parser")
        return None

    with open(extracted_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    print(f"         [AI] Qwen detecting document structure...")

    # ── Phase 1: Extract header fields (title, authors, abstract, keywords) ──
    doc = _extract_header(raw)

    # ── Phase 2: Extract body in batches ─────────────────────────────────────
    body_text = _get_body_text(raw)
    batches   = _split_into_batches(body_text, LM_BATCH_CHARS)
    print(f"         [AI] Processing {len(batches)} batch(es)...")

    all_sections   = []
    all_references = []

    for i, batch in enumerate(batches, 1):
        print(f"         [AI] Batch {i}/{len(batches)}...", end=" ", flush=True)
        result = _extract_body_batch(batch, i, len(batches))
        if result:
            all_sections.extend(result.get("sections", []))
            all_references.extend(result.get("references", []))
            print("✓")
        else:
            print("skipped")

    # ── Phase 3: Merge results ────────────────────────────────────────────────
    doc.sections   = _merge_sections(all_sections)
    doc.references = _deduplicate_refs(all_references)

    print(f"         [AI] ✓ {len(doc.sections)} sections, {len(doc.references)} refs")
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# LM Studio health check
# ─────────────────────────────────────────────────────────────────────────────

def _lm_studio_running() -> bool:
    """Ping LM Studio to check if it's available."""
    try:
        req = urllib.request.Request(
            f"{LM_STUDIO_URL}/models",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Header extraction (title, authors, abstract, keywords)
# ─────────────────────────────────────────────────────────────────────────────

_HEADER_PROMPT = """You are a document structure parser. Extract the header information from this academic document text.

Return ONLY valid JSON with this exact schema (no markdown, no explanation):
{
  "title": "string",
  "authors": [
    {"name": "string", "organization": "string", "email": "string"}
  ],
  "abstract": "string",
  "keywords": ["string"]
}

Rules:
- If a field is not found, use "" for strings or [] for arrays
- authors is a list even for single author
- abstract should be the full abstract text
- keywords should be individual terms, not one long string
- Return ONLY the JSON object, nothing else

Document text (first 3000 chars):
"""

def _extract_header(raw: str) -> Document:
    """Send the first part of the document to extract header fields."""
    header_text = raw[:3000]
    response    = _call_llm(_HEADER_PROMPT + header_text, max_tokens=1000)

    if not response:
        return Document()

    data = _safe_parse_json(response)
    if not data:
        return Document()

    doc = Document()
    doc.title    = data.get("title", "Untitled")
    doc.abstract = data.get("abstract", "")
    doc.keywords = data.get("keywords", [])

    raw_authors  = data.get("authors", [])
    doc.authors  = []
    for a in raw_authors:
        if isinstance(a, dict):
            doc.authors.append(Author(
                name         = a.get("name", ""),
                organization = a.get("organization", ""),
                email        = a.get("email", ""),
            ))
        elif isinstance(a, str):
            doc.authors.append(Author(name=a))

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Body batch extraction (sections, references)
# ─────────────────────────────────────────────────────────────────────────────

_BODY_PROMPT = """You are a document structure parser. Extract sections and references from this text chunk.
This is chunk {batch_num} of {total_batches} from the document.

Return ONLY valid JSON with this exact schema (no markdown, no explanation):
{
  "sections": [
    {"heading": "string", "body": "string"}
  ],
  "references": ["string"]
}

Rules:
- sections: each detected section with its heading and full body text
- heading should be the clean section name without numbering (e.g. "Introduction" not "1. Introduction")
- body should be the complete section text
- references: only include if this chunk contains a references/bibliography section
- Each reference should be one complete citation string
- If no sections or references found in this chunk, return empty arrays
- Return ONLY the JSON object, nothing else

Text chunk:
"""

def _extract_body_batch(text: str, batch_num: int, total: int) -> dict | None:
    """Extract sections and references from one batch."""
    prompt   = _BODY_PROMPT.format(batch_num=batch_num, total_batches=total)
    response = _call_llm(prompt + text, max_tokens=LM_MAX_TOKENS)

    if not response:
        return None

    return _safe_parse_json(response)


# ─────────────────────────────────────────────────────────────────────────────
# LLM call
# ─────────────────────────────────────────────────────────────────────────────

def _call_llm(prompt: str, max_tokens: int = 2048) -> str | None:
    """Send a prompt to LM Studio and return the response text."""
    payload = json.dumps({
        "model":       LM_STUDIO_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.1,    # low temp for deterministic structured output
        "stream":      False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/chat/completions",
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=LM_STUDIO_TIMEOUT) as resp:
            result  = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            return content.strip()
    except urllib.error.URLError as e:
        print(f"\n         [AI] Request failed: {e}")
        return None
    except (KeyError, json.JSONDecodeError) as e:
        print(f"\n         [AI] Parse error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_body_text(raw: str) -> str:
    """Skip the header portion (first ~800 chars) for body extraction."""
    # Find where the body starts — after abstract/keywords
    for marker in ["introduction", "1 introduction", "1. introduction", "i. introduction"]:
        idx = raw.lower().find(marker)
        if idx > 0:
            return raw[idx:]
    # Fallback: skip first 800 chars
    return raw[800:]


def _split_into_batches(text: str, batch_size: int) -> list[str]:
    """
    Split text into overlapping batches of ~batch_size chars.
    Splits on paragraph boundaries to avoid cutting mid-sentence.
    Adds 200-char overlap between batches to avoid missing content at boundaries.
    """
    if len(text) <= batch_size:
        return [text]

    batches = []
    start   = 0
    overlap = 200

    while start < len(text):
        end = start + batch_size

        if end < len(text):
            # Try to split on a paragraph boundary
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + batch_size // 2:
                end = para_break

        batches.append(text[start:end].strip())
        start = max(start + 1, end - overlap)  # overlap for context continuity

    return [b for b in batches if b.strip()]


def _safe_parse_json(text: str) -> dict | None:
    """
    Parse JSON from LLM response.
    Handles common LLM output issues:
      - Markdown code fences ```json ... ```
      - Leading/trailing text before/after the JSON object
      - Thinking tags <think>...</think> from Qwen
    """
    if not text:
        return None

    # Strip Qwen thinking tags
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Try to find a JSON object in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to fix common issues: trailing commas, single quotes
        text = re.sub(r",\s*([}\]])", r"\1", text)   # trailing commas
        text = text.replace("'", '"')                  # single → double quotes
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def _merge_sections(raw_sections: list) -> list[Section]:
    """
    Merge sections from multiple batches.
    Consecutive batches may produce duplicate or split sections — merge them.
    """
    if not raw_sections:
        return []

    sections = []
    for s in raw_sections:
        if not isinstance(s, dict):
            continue
        heading = s.get("heading", "").strip()
        body    = s.get("body", "").strip()
        if not heading or not body:
            continue

        # If same heading as last section, append the body (split across batches)
        if sections and sections[-1].heading.lower() == heading.lower():
            sections[-1] = Section(
                heading = sections[-1].heading,
                body    = sections[-1].body + "\n\n" + body
            )
        else:
            sections.append(Section(heading=heading, body=body))

    return sections


def _deduplicate_refs(refs: list) -> list[str]:
    """Remove duplicate references (same string may appear from overlapping batches)."""
    seen = set()
    result = []
    for r in refs:
        if isinstance(r, str):
            key = r.strip()[:80]   # compare on first 80 chars
            if key and key not in seen:
                seen.add(key)
                result.append(r.strip())
    return result