"""
diagnostic.py — Debug what the parser sees for author extraction.
Usage: python diagnostic.py input/paper.pdf
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    if len(sys.argv) < 2:
        print("Usage: python diagnostic.py input/paper.pdf")
        return

    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        return

    try:
        import fitz
    except ImportError:
        print("ERROR: PyMuPDF (fitz) not installed")
        return

    pdf = fitz.open(path)

    # Determine body_size (same logic as parser)
    all_sizes = []
    all_blocks_info = []

    for page_num in range(min(len(pdf), 3)):  # first 3 pages
        page = pdf[page_num]
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            block_text = ""
            block_fonts = []
            block_sizes = []
            for line in block.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                    block_fonts.append(span.get("font", ""))
                    block_sizes.append(span.get("size", 0))
                block_text += line_text + "\n"
            block_text = block_text.strip()
            if not block_text:
                continue
            font = max(set(block_fonts), key=block_fonts.count) if block_fonts else ""
            size = max(set(block_sizes), key=block_sizes.count) if block_sizes else 0
            all_sizes.append(size)
            all_blocks_info.append({
                "text": block_text,
                "font": font,
                "size": round(size, 1),
                "page": page_num,
            })

    pdf.close()

    if not all_sizes:
        print("No text blocks found!")
        return

    body_size = max(set(all_sizes), key=all_sizes.count)
    print(f"Body size: {body_size}")
    print(f"Total blocks (first 3 pages): {len(all_blocks_info)}")
    print()

    # Show first 35 blocks (what author extraction sees)
    print("=" * 80)
    print("FIRST 35 BLOCKS (what _extract_authors_from_blocks scans)")
    print("=" * 80)
    for i, b in enumerate(all_blocks_info[:35]):
        text_preview = b["text"][:120].replace("\n", "\\n")
        is_large = b["size"] > body_size * 1.2
        lower = b["text"].strip().lower()

        # Classify what the parser would do with this block
        classification = ""
        if is_large:
            classification = ">> TITLE-SIZED (triggers author zone)"
        elif lower.strip() in ("authors", "author"):
            classification = ">> AUTHORS HEADING (triggers author zone)"
        elif lower.startswith("abstract"):
            classification = ">> ABSTRACT (stops author scan)"
        elif lower.startswith("keywords"):
            classification = ">> KEYWORDS (stops author scan)"

        print(f"  [{i:2d}] size={b['size']:5.1f}  font={b['font'][:25]:<25s}  "
              f"{'LARGE' if is_large else '     '}")
        print(f"       text: {text_preview}")
        if classification:
            print(f"       {classification}")
        print()

    # Now actually run the author extraction
    print("=" * 80)
    print("RUNNING AUTHOR EXTRACTION")
    print("=" * 80)
    from parser.heuristic import _extract_authors_from_blocks
    authors = _extract_authors_from_blocks(all_blocks_info, body_size)
    if authors:
        for a in authors:
            print(f"  Author: {a.name}")
            if a.department: print(f"    dept: {a.department}")
            if a.organization: print(f"    org:  {a.organization}")
            if a.country: print(f"    country: {a.country}")
            if a.email: print(f"    email: {a.email}")
    else:
        print("  NO AUTHORS FOUND!")
        print()
        print("  Debugging: scanning for 'author' in block text...")
        for i, b in enumerate(all_blocks_info[:35]):
            if "author" in b["text"].lower():
                print(f"    Block [{i}]: \"{b['text'][:100]}\" (size={b['size']})")


if __name__ == "__main__":
    main()
