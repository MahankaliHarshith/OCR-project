"""
Training Pipeline for OCR Receipt Scanner.

Provides tools to:
    1. Ingest labeled receipt images (ground truth)
    2. Benchmark OCR accuracy against known-correct data
    3. Auto-tune OCR parameters for maximum accuracy
    4. Learn receipt template layouts for faster processing
"""
