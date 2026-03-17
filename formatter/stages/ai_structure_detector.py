"""
stages/ai_structure_detector.py
Stage 2A (AI path) — raw text + rich layout dict → Document via LM Studio (Qwen)

Changes vs old version:
  - Accepts rich dict from layout_parser, injects pre-extracted tables
  - Header prompt now extracts department/city/country per author
  - Body prompt schema now includes tables[] with structured fields
  - References returned as {index, text} objects not plain strings
  - _deduplicate_refs works on Reference objects
  - Tables from layout_parser injected via _inject_tables() (same as heuristic)
"""

import json
import re
import urllib.request
import urllib.error
from core.models import Document, Section, Author, Table, Reference
from core.config  import (LM_STUDIO_URL, LM_STUDIO_MODEL, LM_STUDIO_TIMEOUT,
                           LM_STUDIO_ENABLED, LM_BATCH_CHARS, LM_MAX_TOKENS)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse(extracted_path: str, rich: dict = None) -> "Document | None":
    """
    Try to parse the extracted text with LM Studio.
    Returns a Document on success, None if LM Studio is unavailable.
    rich: layout dict with pre-extracted tables[] from pdfplumber.
    """
    if not LM_STUDIO_ENABLED:
        return None

    if not _lm_studio_running():
        print("         [AI] LM Studio not running — using heuristic parser")
        return None

    with open(extracted_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    print("         [AI] Qwen detecting document structure...")

    doc = _extract_header(raw)

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

    doc.sections = _merge_sections(all_sections)

    # If LLM returned few/no refs, fall back to heuristic ref extraction
    # (LLM often misses refs that are split across batch boundaries)
    if len(all_references) < 3:
        print("         [AI] Few refs from LLM — using heuristic ref extractor")
        from stages.document_parser import _extract_references_from_text
        heuristic_refs = _extract_references_from_text(raw)
        if len(heuristic_refs) > len(all_references):
            all_references = [{"index": r.index, "text": r.text} for r in heuristic_refs]

    doc.references = _deduplicate_refs(all_references)

    # Inject pdfplumber tables into sections
    if rich and rich.get("tables"):
        from stages.document_parser import _inject_tables
        _inject_tables(doc, rich["tables"])

    print(f"         [AI] ✓ {len(doc.sections)} sections, "
          f"{sum(len(s.tables) for s in doc.sections)} tables, "
          f"{len(doc.references)} refs")
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# LM Studio health check
# ─────────────────────────────────────────────────────────────────────────────

def _lm_studio_running() -> bool:
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
# Header extraction prompt
# ─────────────────────────────────────────────────────────────────────────────

_HEADER_PROMPT = """\
You are a document structure parser. Extract header information from this academic paper.

Return ONLY a single valid JSON object — no markdown fences, no explanation, nothing else.

Schema:
{
  "title": "full paper title as a single string",
  "authors": [
    {
      "name": "Author Full Name",
      "department": "Department name or empty string",
      "organization": "University or institution name or empty string",
      "city": "City, Country or empty string",
      "email": "email@domain.com or empty string"
    }
  ],
  "abstract": "full abstract text as a single string",
  "keywords": ["keyword1", "keyword2"]
}

Rules:
- title: extract the actual paper title, not section headings
- authors: list ALL authors found; if only names visible, set other fields to ""
- abstract: include the full abstract, not truncated
- keywords: split on commas/semicolons into individual terms
- Empty string "" for missing fields, [] for missing arrays
- Return ONLY the JSON, nothing else

Document text (first 3000 characters):
"""


def _extract_header(raw: str) -> Document:
    response = _call_llm(_HEADER_PROMPT + raw[:3000], max_tokens=1000)
    if not response:
        return Document()

    data = _safe_parse_json(response)
    if not data:
        return Document()

    doc          = Document()
    doc.title    = data.get("title", "Untitled")
    doc.abstract = data.get("abstract", "")
    doc.keywords = data.get("keywords", [])
    doc.authors  = []

    for a in data.get("authors", []):
        if isinstance(a, dict):
            doc.authors.append(Author(
                name         = a.get("name", ""),
                department   = a.get("department", ""),
                organization = a.get("organization", ""),
                city         = a.get("city", ""),
                country      = a.get("country", ""),
                email        = a.get("email", ""),
            ))
        elif isinstance(a, str):
            doc.authors.append(Author(name=a))

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Body batch extraction prompt
# ─────────────────────────────────────────────────────────────────────────────

_BODY_PROMPT = """\
You are a document structure parser. Extract sections and references from this text chunk.
This is chunk {batch_num} of {total_batches} from an academic paper.

Return ONLY a single valid JSON object — no markdown fences, no explanation, nothing else.

Schema:
{
  "sections": [
    {
      "heading": "Section Name (clean, no numbering like '1.' or 'I.')",
      "body": "full section body text as a single string"
    }
  ],
  "references": [
    {
      "index": 1,
      "text": "Full citation text without the [N] prefix"
    }
  ]
}

Rules:
- sections: each section with its heading and complete body text
- heading: clean section name only, strip leading numbers/roman numerals
- body: complete text of the section, preserve all sentences
- references: ONLY include if this chunk has a References/Bibliography section
- references.index: the citation number from [N] or (N) at start of entry
- references.text: full citation string without the [N] prefix
- If no sections or references in this chunk, return empty arrays []
- Return ONLY the JSON, nothing else

Text chunk:
"""


def _extract_body_batch(text: str, batch_num: int, total: int) -> dict:
    prompt   = _BODY_PROMPT.format(batch_num=batch_num, total_batches=total)
    response = _call_llm(prompt + text, max_tokens=LM_MAX_TOKENS)
    if not response:
        return None
    return _safe_parse_json(response)


# ─────────────────────────────────────────────────────────────────────────────
# LLM call
# ─────────────────────────────────────────────────────────────────────────────

def _call_llm(prompt: str, max_tokens: int = 2048) -> "str | None":
    payload = json.dumps({
        "model":       LM_STUDIO_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.1,
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
    for marker in ["introduction", "1 introduction", "1. introduction", "i. introduction"]:
        idx = raw.lower().find(marker)
        if idx > 0:
            return raw[idx:]
    return raw[800:]


def _split_into_batches(text: str, batch_size: int) -> list:
    if len(text) <= batch_size:
        return [text]

    batches = []
    start   = 0
    overlap = 200

    while start < len(text):
        end = start + batch_size
        if end < len(text):
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + batch_size // 2:
                end = para_break
        batches.append(text[start:end].strip())
        start = max(start + 1, end - overlap)

    return [b for b in batches if b.strip()]


def _safe_parse_json(text: str) -> "dict | None":
    if not text:
        return None

    # Strip Qwen thinking tags
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # Extract JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fix trailing commas and single quotes
        text = re.sub(r",\s*([}\]])", r"\1", text)
        text = text.replace("'", '"')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def _merge_sections(raw_sections: list) -> list:
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

        if sections and sections[-1].heading.lower() == heading.lower():
            sections[-1] = Section(
                heading=sections[-1].heading,
                body=sections[-1].body + "\n\n" + body,
                tables=sections[-1].tables,
                figures=sections[-1].figures,
            )
        else:
            sections.append(Section(heading=heading, body=body))

    return sections


def _deduplicate_refs(refs: list) -> list:
    """
    Deduplicate Reference objects.
    refs items may be dicts {index, text} from LLM, or Reference objects.
    Returns list of Reference objects.
    """
    seen   = set()
    result = []

    for r in refs:
        if isinstance(r, dict):
            idx  = int(r.get("index", len(result) + 1))
            text = str(r.get("text", "")).strip()
        elif isinstance(r, Reference):
            idx  = r.index
            text = r.text.strip()
        elif isinstance(r, str):
            idx  = len(result) + 1
            text = r.strip()
        else:
            continue

        if not text:
            continue

        key = text[:80]
        if key not in seen:
            seen.add(key)
            result.append(Reference(index=idx, text=text))

    # Sort by index
    result.sort(key=lambda r: r.index)
    return result