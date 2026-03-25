#!/usr/bin/env python3
"""
extractor/nougat_worker.py — Subprocess worker for Meta's Nougat OCR.

Usage:
    python nougat_worker.py <image_path>

Prints LaTeX string to stdout.  Small equation crops are padded to
896×1152 (nougat's expected page dimensions).
"""
import sys


def _pad_to_page(img):
    """Pad a small crop to nougat's expected page size."""
    from PIL import Image
    target_w, target_h = 896, 1152
    w, h = img.size
    if w >= target_w and h >= target_h:
        return img
    new = Image.new("RGB", (max(w, target_w), max(h, target_h)), (255, 255, 255))
    new.paste(img, (0, 0))
    return new


def main():
    if len(sys.argv) < 2:
        print("Usage: nougat_worker.py <image_path>", file=sys.stderr)
        sys.exit(1)

    image_path = sys.argv[1]

    try:
        from PIL import Image
        from nougat import NougatModel
        from nougat.utils.checkpoint import get_checkpoint
        import torch

        ckpt = get_checkpoint("nougat-0.1.0-small")
        model = NougatModel.from_pretrained(ckpt)
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()

        img = Image.open(image_path).convert("RGB")
        img = _pad_to_page(img)

        # nougat expects a specific input format
        from nougat.utils.dataset import ImageDataset
        sample = ImageDataset.ignore_none_collate([model.encoder.prepare_input(img)])
        if torch.cuda.is_available():
            sample = sample.cuda()

        with torch.no_grad():
            output = model.inference(image_tensors=sample)

        # output is a dict with 'predictions'
        if output and "predictions" in output:
            latex = output["predictions"][0]
            print(latex, end="")
        else:
            print("", end="")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
