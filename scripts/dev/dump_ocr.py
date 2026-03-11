"""Dump all OCR detections for Media (2).jpg to diagnose qty accuracy."""
import sys, io, warnings, os
warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["PYTHONIOENCODING"] = "utf-8"

from app.ocr.engine import OCREngine
from app.ocr.preprocessor import ImagePreprocessor

prep = ImagePreprocessor()
eng = OCREngine()

for img_path in ["uploads/upload_20260222_213048.png"]:
    print(f"\n{'='*60}")
    print(f"  {img_path}")
    print(f"{'='*60}")

    import cv2
    raw = cv2.imread(img_path)
    print(f"  Raw image shape: {raw.shape if raw is not None else 'FAILED TO LOAD'}")

    gray_img, meta = prep.preprocess(img_path)
    print(f"  Preprocessed shape: {gray_img.shape}")
    print(f"  Metadata: {meta}")

    gray_results = eng.extract_text(gray_img)
    print(f"\n--- GRAY PASS ({len(gray_results)} detections) ---")
    for i, d in enumerate(gray_results):
        y_mid = int((d["bbox"][0][1] + d["bbox"][2][1]) / 2)
        x_mid = int((d["bbox"][0][0] + d["bbox"][2][0]) / 2)
        print(f"  [{i+1:2d}] x={x_mid:4d} y={y_mid:4d}  conf={d['confidence']:.4f}  text={d['text']!r}")
