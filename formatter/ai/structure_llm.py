"""
ai/structure_llm.py  —  Stage 2A: raw text → Document via LM Studio (Qwen)

Timeout fix:
  urllib.urlopen(timeout=N) sets socket.settimeout(N) which fires on EVERY
  individual recv() call — not just the first token. If the model pauses >N
  seconds between any two SSE chunks (e.g. thinking before starting JSON),
  you get TimeoutError mid-stream even though the model is still working.

  Fix: use requests with timeout=(connect_timeout, None).
    - connect_timeout (60s): how long to wait for the TCP handshake + HTTP 200
    - None: no timeout on individual reads — wait as long as the model needs

  The model is confirmed working (header call succeeded at 350 tokens).
  Body batches just need more think-time before the first JSON token appears.
"""

import json, re, time
from core.models  import Document, Section, Author, Reference
from core.config  import (LM_STUDIO_URL, LM_STUDIO_MODEL, LM_STUDIO_TIMEOUT,
                           LM_STUDIO_ENABLED, LM_BATCH_CHARS, LM_MAX_TOKENS)
from core.logger  import get_logger

log = get_logger(__name__)

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    log.warning("requests library not found — falling back to urllib (timeout issues may occur). "
                "Run: pip install requests")


def parse(extracted_path: str, rich: dict) -> "Document | None":
    if not LM_STUDIO_ENABLED or not _running():
        print("         [AI] LM Studio not running — using heuristic parser")
        return None

    with open(extracted_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    print("         [AI] Detecting structure with Qwen...")
    log.info(f"AI parse start — {len(raw)} chars, model={LM_STUDIO_MODEL}, "
             f"backend={'requests' if _HAS_REQUESTS else 'urllib'}")

    doc = _extract_header(raw)

    batches = _batches(_body_text(raw), LM_BATCH_CHARS)
    n = len(batches)
    print(f"         [AI] {n} batch(es) @ ~{LM_BATCH_CHARS//1000}K chars each")
    log.info(f"Batches: {n}  batch_size={LM_BATCH_CHARS}")

    all_sections, all_refs = [], []

    for i, batch in enumerate(batches, 1):
        print(f"         [AI] batch {i}/{n} ({len(batch)} chars)... ", end="", flush=True)
        log.info(f"Batch {i}/{n} start — {len(batch)} chars")
        t0  = time.time()
        res = _extract_body(batch, i, n)
        elapsed = time.time() - t0

        if res:
            secs = res.get("sections", [])
            refs = res.get("references", [])
            all_sections.extend(secs)
            all_refs.extend(refs)
            print(f"✓  {len(secs)} sections, {len(refs)} refs  ({elapsed:.1f}s)")
            log.info(f"Batch {i}/{n} OK — {len(secs)} sections, {len(refs)} refs, {elapsed:.1f}s")
        else:
            print(f"skip  ({elapsed:.1f}s)")
            log.warning(f"Batch {i}/{n} returned no usable JSON — {elapsed:.1f}s")

    doc.sections = _merge_sections(all_sections)

    if len(all_refs) < 3:
        log.info("Fewer than 3 refs from LLM — running heuristic ref extractor")
        from ai.heuristic_parser import extract_references
        hr = extract_references(raw)
        if len(hr) > len(all_refs):
            log.info(f"Heuristic extractor found {len(hr)} refs")
            all_refs = [{"index": r.index, "text": r.text} for r in hr]

    doc.references = _dedup_refs(all_refs)
    _inject_tables(doc, rich.get("tables", []))

    log.info(f"AI parse complete — {len(doc.sections)} sections, "
             f"{sum(len(s.tables) for s in doc.sections)} tables, "
             f"{len(doc.references)} refs")
    print(f"         [AI] ✓ {len(doc.sections)} sections | "
          f"{sum(len(s.tables) for s in doc.sections)} tables | "
          f"{len(doc.references)} refs")
    return doc


# ── Prompts ───────────────────────────────────────────────────────────────────

_HEADER_PROMPT = """\
Extract header info from this academic paper. Return ONLY valid JSON, no markdown.

{
  "title": "full paper title",
  "authors": [{"name":"","department":"","organization":"","city":"","country":"","email":""}],
  "abstract": "full abstract",
  "keywords": ["kw1","kw2"]
}

Rules: all fields required, use "" or [] if missing. Return ONLY the JSON.

Text (first 3000 chars):
"""

_BODY_PROMPT = """You are a JSON extraction tool. Read chunk {n} of {total} from an academic paper.

Output ONLY a raw JSON object. No explanation. No markdown. No preamble. No schema description.
Start your response with the character {{ and end with }}.

Required keys:
- "sections": list of {{"heading": "...", "body": "..."}} — heading has no leading numbers
- "references": list of {{"index": N, "text": "..."}} — only if chunk has References section, else []

Chunk text:
"""



def _extract_header(raw: str) -> Document:
    log.info("Extracting header (first 3000 chars)")
    res  = _call(_HEADER_PROMPT + raw[:3000], max_tokens=3000, label="header")
    data = _parse_json(res) if res else None
    if not data:
        log.warning("Header extraction returned no usable JSON")
        return Document()
    doc          = Document()
    doc.title    = data.get("title", "Untitled")
    doc.abstract = data.get("abstract", "")
    doc.keywords = data.get("keywords", [])
    doc.authors  = [
        Author(**{k: a.get(k,"") for k in
                  ("name","department","organization","city","country","email")})
        if isinstance(a, dict) else Author(name=str(a))
        for a in data.get("authors", [])
    ]
    log.info(f"Header: title={repr(doc.title[:60])}, {len(doc.authors)} authors")
    return doc


def _extract_body(text: str, n: int, total: int) -> "dict | None":
    prompt = _BODY_PROMPT.format(n=n, total=total)
    res    = _call(prompt + text, max_tokens=LM_MAX_TOKENS, label=f"b{n}")
    return _parse_json(res) if res else None


# ── LLM call — requests (preferred) or urllib fallback ────────────────────────

def _call(prompt: str, max_tokens: int, label: str = "") -> "str | None":
    if _HAS_REQUESTS:
        return _call_requests(prompt, max_tokens, label)
    return _call_urllib(prompt, max_tokens, label)


def _call_requests(prompt: str, max_tokens: int, label: str) -> "str | None":
    """
    Stream via requests.
    timeout=(LM_STUDIO_TIMEOUT, None):
      - LM_STUDIO_TIMEOUT seconds to connect + receive HTTP 200
      - None = no per-read timeout, model can take as long as it needs per token
    This is the correct fix for the urllib TimeoutError on body batches.
    """
    payload = {
        "model":       LM_STUDIO_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.1,
        "stream":      True,
    }

    full_text   = []
    token_count = 0

    try:
        resp = _requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json=payload,
            stream=True,
            timeout=(LM_STUDIO_TIMEOUT, None),  # (connect_timeout, read_timeout=unlimited)
        )
        resp.raise_for_status()

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk   = json.loads(data_str)
                content = chunk["choices"][0]["delta"].get("content", "")
                if content:
                    full_text.append(content)
                    token_count += 1
                    if token_count % 50 == 0:
                        print(f"\r         [AI] {label} {token_count} tokens...",
                              end="", flush=True)
            except (json.JSONDecodeError, KeyError):
                continue

        result = "".join(full_text)
        log.debug(f"[{label}] done — {token_count} tokens")
        return result if result.strip() else None

    except _requests.exceptions.ConnectionError as e:
        log.error(f"[{label}] connection error: {e}")
        print(f"\n         [AI] connection error: {e}", flush=True)
        return None
    except _requests.exceptions.Timeout as e:
        log.error(f"[{label}] connect timeout ({LM_STUDIO_TIMEOUT}s) — "
                  "LM Studio didn't respond in time. Is it running?")
        print(f"\n         [AI] connect timeout: {e}", flush=True)
        return None
    except Exception as e:
        log.error(f"[{label}] unexpected error: {e}", exc_info=True)
        print(f"\n         [AI] error: {e}", flush=True)
        return None


def _call_urllib(prompt: str, max_tokens: int, label: str) -> "str | None":
    """Fallback when requests isn't installed. Has the per-read timeout limitation."""
    import urllib.request, urllib.error

    log.warning(f"[{label}] using urllib fallback — install requests to fix timeout issues")
    payload = json.dumps({
        "model": LM_STUDIO_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.1, "stream": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/chat/completions", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")

    full_text, token_count = [], 0
    try:
        with urllib.request.urlopen(req, timeout=LM_STUDIO_TIMEOUT) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk   = json.loads(data_str)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content:
                        full_text.append(content)
                        token_count += 1
                        if token_count % 50 == 0:
                            print(f"\r         [AI] {label} {token_count} tokens...",
                                  end="", flush=True)
                except (json.JSONDecodeError, KeyError):
                    continue
        result = "".join(full_text)
        return result if result.strip() else None
    except Exception as e:
        log.error(f"[{label}] error: {e}", exc_info=True)
        print(f"\n         [AI] error: {e}", flush=True)
        return None


# ── Health check ──────────────────────────────────────────────────────────────

def _running() -> bool:
    try:
        if _HAS_REQUESTS:
            _requests.get(f"{LM_STUDIO_URL}/models", timeout=3)
        else:
            import urllib.request
            urllib.request.urlopen(f"{LM_STUDIO_URL}/models", timeout=3)
        return True
    except Exception:
        return False


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> "dict | None":
    if not text:
        return None

    # Strip Qwen <think> blocks — handle BOTH closed and truncated (no </think>)
    # Truncated case: model hit token limit mid-thought → entire response is thinking
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)  # unclosed tag
    text = text.strip()

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    # Fix raw newlines inside JSON string values (Qwen sometimes outputs these).
    # Replace literal newlines inside quoted strings with the \n escape sequence.
    def _fix_newlines(m):
        return m.group(0).replace("\n", "\\n").replace("\r", "\\r")
    text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', _fix_newlines, text, flags=re.DOTALL)

    # Extract balanced JSON object — finds first { then matches its closing }
    start = text.find("{")
    if start == -1:
        log.warning(f"No JSON object found — head: {text[:200]!r}")
        return None
    depth, i = 0, start
    in_str, escape = False, False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False; continue
        if ch == "\\" and in_str:
            escape = True; continue
        if ch == '"' and not escape:
            in_str = not in_str; continue
        if in_str:
            continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i+1]
                break
    else:
        candidate = text[start:]

    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    log.warning(f"JSON parse failed — head: {text[:200]!r}")
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _body_text(raw: str) -> str:
    for marker in ["introduction", "1 introduction", "1. introduction"]:
        idx = raw.lower().find(marker)
        if idx > 0:
            return raw[idx:]
    return raw[800:]


def _batches(text: str, size: int) -> list:
    if len(text) <= size:
        return [text]
    result, start, overlap = [], 0, 200
    while start < len(text):
        end = start + size
        if end < len(text):
            pb = text.rfind("\n\n", start, end)
            if pb > start + size // 2:
                end = pb
        result.append(text[start:end].strip())
        start = max(start + 1, end - overlap)
    return [b for b in result if b.strip()]


def _merge_sections(raw: list) -> list:
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        h, b = s.get("heading","").strip(), s.get("body","").strip()
        if not h or not b:
            continue
        if out and out[-1].heading.lower() == h.lower():
            out[-1].body += "\n\n" + b
        else:
            out.append(Section(heading=h, body=b))
    return out


def _dedup_refs(refs: list) -> list:
    seen, out = set(), []
    for r in refs:
        if isinstance(r, dict):
            idx, text = int(r.get("index", len(out)+1)), str(r.get("text","")).strip()
        elif isinstance(r, Reference):
            idx, text = r.index, r.text.strip()
        else:
            continue
        key = text[:80]
        if key and key not in seen:
            seen.add(key)
            out.append(Reference(index=idx, text=text))
    out.sort(key=lambda r: r.index)
    return out


def _inject_tables(doc: Document, raw_tables: list):
    if not raw_tables or not doc.sections:
        return
    from mapper.base_mapper import inject_tables
    inject_tables(doc, raw_tables)