"""
extractor/docx_extractor.py  —  DOCX → {raw_text, tables, images}

Uses python-docx to walk the document body in order (paragraphs + tables
interleaved), preserving document structure.
Per the DOCX skill: python-docx is recommended for reading .docx files.
"""

import os, re


def extract(input_path: str, intermediate_dir: str) -> dict:
    """
    Extract text, tables, and images from a DOCX.
    Always returns: {"raw_text": str, "tables": list, "images": list}
    """
    result = {"raw_text": "", "tables": [], "images": []}
    try:
        result = _extract_docx(input_path, intermediate_dir)
    except Exception as e:
        print(f"         [extractor] DOCX extraction failed: {e}")

    _write_outputs(result, intermediate_dir)
    print(f"         [docx] {len(result['raw_text'])} chars | "
          f"{len(result['tables'])} tables | {len(result['images'])} images")
    return result


def _extract_docx(input_path: str, intermediate_dir: str) -> dict:
    from docx import Document as DocxDoc

    doc        = DocxDoc(input_path)
    body       = doc.element.body
    para_idx   = 0
    table_idx  = 0
    text_parts = []
    tables     = []

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            if para_idx < len(doc.paragraphs):
                para = doc.paragraphs[para_idx]
                para_idx += 1
                text = para.text.strip()
                if not text:
                    continue
                # Detect equation blocks: [$latex$]
                eq = re.match(r"^\[\s*\n?(.*?)\n?\s*\]$", text, re.DOTALL)
                if eq:
                    latex = eq.group(1).strip().replace("\\\\", "\\")
                    text_parts.append(f"$$\n{latex}\n$$")
                else:
                    text_parts.append(text)

        elif tag == "tbl":
            if table_idx < len(doc.tables):
                tbl = doc.tables[table_idx]
                table_idx += 1
                rows_raw = []
                for row in tbl.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    rows_raw.append(cells)

                if rows_raw:
                    n       = max(len(r) for r in rows_raw)
                    headers = (rows_raw[0] + [""] * n)[:n]
                    rows    = [(r   + [""] * n)[:n] for r in rows_raw[1:]]
                    # Caption: look back in text_parts for "Table N ..."
                    cap = ""
                    for prev in reversed(text_parts[-3:]):
                        if re.match(r"^Table\s+\d+", prev, re.IGNORECASE) or \
                           (len(prev.split()) <= 8 and not prev.startswith("$$")):
                            cap = prev
                            break
                    tables.append({
                        "caption": cap,
                        "headers": headers,
                        "rows":    rows,
                        "page":    0,
                        "notes":   "",
                    })

    return {
        "raw_text": "\n\n".join(text_parts),
        "tables":   tables,
        "images":   [],
    }


def _write_outputs(result: dict, intermediate_dir: str):
    import json
    os.makedirs(intermediate_dir, exist_ok=True)
    with open(os.path.join(intermediate_dir, "extracted.txt"), "w", encoding="utf-8") as f:
        f.write(result["raw_text"])
    with open(os.path.join(intermediate_dir, "extracted_rich.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)