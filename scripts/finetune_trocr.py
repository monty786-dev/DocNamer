"""EXPERIMENTAL / UNTESTED - fine-tune TrOCR on handwriting exported from the library.

    python -m docnamer library export-dataset my_set --format trocr      # creates my_set/metadata.jsonl + images/
    pip install torch transformers datasets pillow
    python scripts/finetune_trocr.py my_set out_model --epochs 5

NOTE: microsoft/trocr-*-handwritten is an ENGLISH model (its tokenizer cannot produce Devanagari). For Hindi,
train a PaddleOCR Devanagari recognition model from the `--format paddle` export (rec_gt.txt), convert it to ONNX
and drop rec.onnx + charset.txt into <data>/models/handwriting/. The built-in 'teach' library works for both
scripts without any training."""
import json
import sys
from pathlib import Path


def main() -> None:
    import torch
    from PIL import Image
    from torch.utils.data import DataLoader, Dataset
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    epochs = int(sys.argv[sys.argv.index("--epochs") + 1]) if "--epochs" in sys.argv else 3
    rows = [json.loads(l) for l in (src / "metadata.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    proc = TrOCRProcessor.from_pretrained("microsoft/trocr-small-handwritten")
    model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-small-handwritten")
    model.config.decoder_start_token_id = proc.tokenizer.cls_token_id
    model.config.pad_token_id = proc.tokenizer.pad_token_id

    class DS(Dataset):
        def __len__(self):
            return len(rows)

        def __getitem__(self, i):
            im = Image.open(src / rows[i]["file_name"]).convert("RGB")
            pv = proc(images=im, return_tensors="pt").pixel_values[0]
            ids = proc.tokenizer(rows[i]["text"], padding="max_length", max_length=32, truncation=True).input_ids
            ids = [t if t != proc.tokenizer.pad_token_id else -100 for t in ids]
            return {"pixel_values": pv, "labels": torch.tensor(ids)}

    dl = DataLoader(DS(), batch_size=4, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-5)
    model.train()
    for ep in range(epochs):
        for batch in dl:
            loss = model(**batch).loss
            loss.backward()
            opt.step()
            opt.zero_grad()
        print(f"epoch {ep + 1}/{epochs} loss {loss.item():.4f}")
    model.save_pretrained(out)
    proc.save_pretrained(out)
    print("saved", out, "- export to ONNX with:  optimum-cli export onnx --model", out, out / "onnx")


if __name__ == "__main__":
    main()
