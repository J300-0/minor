#!/usr/bin/env python3
"""
extractor/nougat_batch_worker.py — Batch subprocess worker for Meta's Nougat OCR.

Usage:
    python nougat_batch_worker.py <image_path_1> <image_path_2> ...

Loads the model ONCE, then OCRs all images in sequence.
Outputs a JSON array of results to stdout:
    [{"path": "...", "latex": "..."}, ...]

Small equation crops are padded to 896x1152 (nougat's expected page dims).
Falls back gracefully: if a single image fails, its entry has latex="".
Runs in a child process so segfaults cannot kill the main pipeline.
"""
import json
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
        print("Usage: nougat_batch_worker.py <image_path> [<image_path> ...]",
              file=sys.stderr)
        sys.exit(1)

    image_paths = sys.argv[1:]

    try:
        from PIL import Image
        from nougat import NougatModel
        from nougat.utils.checkpoint import get_checkpoint
        from nougat.utils.dataset import ImageDataset
        import torch

        # Load model ONCE — this is the expensive step
        ckpt = get_checkpoint("nougat-0.1.0-small")
        model = NougatModel.from_pretrained(ckpt)
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()

        results = []
        for path in image_paths:
            try:
                img = Image.open(path).convert("RGB")
                img = _pad_to_page(img)

                sample = ImageDataset.ignore_none_collate(
                    [model.encoder.prepare_input(img)]
                )
                if torch.cuda.is_available():
                    sample = sample.cuda()

                with torch.no_grad():
                    output = model.inference(image_tensors=sample)

                latex = ""
                if output and "predictions" in output:
                    latex = output["predictions"][0] or ""

                results.append({"path": path, "latex": latex})
            except Exception as e:
                print(f"WARN: Failed on {path}: {e}", file=sys.stderr)
                results.append({"path": path, "latex": ""})

        print(json.dumps(results), end="")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
