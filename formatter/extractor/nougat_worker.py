"""
extractor/nougat_worker.py — Isolated Nougat OCR worker (Meta).

Called by pdf_extractor.py via subprocess:
    python nougat_worker.py <image_path>

Prints the LaTeX/markdown result to stdout (one line).
Exits 0 on success, 1 on failure.

Nougat is designed for full scientific pages, so small equation crops
are padded to page-like dimensions before inference.

Install: pip install nougat-ocr
"""
import os
import sys
import re


def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    image_path = sys.argv[1]
    if not os.path.exists(image_path):
        print(f"File not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    try:
        import torch
        from PIL import Image
        from nougat import NougatModel
        from nougat.utils.checkpoint import get_checkpoint
        from nougat.utils.dataset import ImageDataset

        # Load model (small variant — faster, lower memory)
        ckpt = get_checkpoint("nougat-0.1.0-small", model_tag="0.1.0-small")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = NougatModel.from_pretrained(ckpt)
        model.to(device)
        model.eval()

        # Load and pad image to page-like dimensions
        img = Image.open(image_path).convert("RGB")

        # Nougat expects ~page-sized input; pad small crops onto white canvas
        page_w, page_h = 896, 1152  # nougat's expected input size
        if img.width < page_w or img.height < page_h:
            padded = Image.new("RGB", (page_w, page_h), (255, 255, 255))
            x = (page_w - img.width) // 2
            y = (page_h - img.height) // 2
            padded.paste(img, (x, y))
            img = padded

        # Prepare for model — use the encoder's preprocessor
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name)
            tmp_path = tmp.name

        dataset = ImageDataset(
            [tmp_path],
            model.encoder.prepare_input,
        )
        dataloader = torch.utils.data.DataLoader(
            dataset, batch_size=1, shuffle=False,
        )

        result_text = ""
        for batch in dataloader:
            batch = batch.to(device)
            with torch.no_grad():
                output = model.inference(image_tensors=batch)
            if output and output[0]:
                result_text = output[0].strip()
            break

        os.unlink(tmp_path)

        if not result_text:
            sys.exit(1)

        # Extract LaTeX from nougat's markdown output
        # Nougat outputs: inline $...$, display \[...\], or \begin{...}
        latex = _extract_latex(result_text)
        if latex:
            print(latex)
            sys.exit(0)
        else:
            # If no delimited math, but output has math-like content, use as-is
            if any(c in result_text for c in r"\{}^_"):
                print(result_text)
                sys.exit(0)
            sys.exit(1)

    except Exception as e:
        print(f"nougat error: {e}", file=sys.stderr)
        sys.exit(1)


def _extract_latex(text: str) -> str:
    """Extract the first LaTeX expression from nougat's markdown output."""
    # Display math: \[...\] or $$...$$
    m = re.search(r'\\\[(.*?)\\\]', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    m = re.search(r'\$\$(.*?)\$\$', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Inline math: $...$
    m = re.search(r'\$([^$]{2,})\$', text)
    if m:
        return m.group(1).strip()

    # Environment: \begin{equation}...\end{equation}, \begin{align}...\end{align}
    m = re.search(r'\\begin\{(equation|align|gather|array)\*?\}(.*?)\\end\{\1\*?\}',
                  text, re.DOTALL)
    if m:
        return m.group(2).strip()

    return ""


if __name__ == "__main__":
    main()
