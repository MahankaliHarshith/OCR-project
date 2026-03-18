#!/usr/bin/env python3
"""
Real-World Receipt Trainer — Interactive CLI.

An intelligent training utility that closes the feedback loop between OCR
scanning and accuracy improvement.  Provides an interactive scan → review →
correct → learn workflow for production receipt scanning.

Usage:
    python -m scripts.trainer scan     <image>        # Scan & label one receipt
    python -m scripts.trainer batch-scan <folder>      # Bulk scan a folder
    python -m scripts.trainer analyze                  # Mine OCR error patterns
    python -m scripts.trainer learn                    # Generate substitution rules
    python -m scripts.trainer confusion                # Show confusion matrix
    python -m scripts.trainer auto-improve             # Full improvement cycle
    python -m scripts.trainer report                   # Training progress report
    python -m scripts.trainer augment [--variations N] # Augment training images
    python -m scripts.trainer status                   # Quick status overview

Examples:
    # Scan a receipt, review what OCR detected, correct mistakes
    python -m scripts.trainer scan receipts/receipt_001.jpg

    # Batch-import a folder of receipts for review
    python -m scripts.trainer batch-scan ./real_receipts/

    # Run full auto-improvement cycle
    python -m scripts.trainer auto-improve --verbose

    # Generate a training progress report
    python -m scripts.trainer report
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

# ─── Ensure project root is on sys.path ──────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ═════════════════════════════════════════════════════════════════════════════
#  COLOUR / FORMATTING HELPERS (works on Windows 10+ ANSI terminals)
# ═════════════════════════════════════════════════════════════════════════════

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
RESET = "\033[0m"


def _header(text: str) -> None:
    width = max(len(text) + 4, 60)
    print(f"\n{CYAN}{'═' * width}")
    print(f"  {BOLD}{text}{RESET}{CYAN}")
    print(f"{'═' * width}{RESET}")


def _section(text: str) -> None:
    print(f"\n{YELLOW}── {text} ──{RESET}")


def _success(text: str) -> None:
    print(f"{GREEN}✔ {text}{RESET}")


def _warning(text: str) -> None:
    print(f"{YELLOW}⚠ {text}{RESET}")


def _error(text: str) -> None:
    print(f"{RED}✘ {text}{RESET}")


def _info(text: str) -> None:
    print(f"{DIM}  {text}{RESET}")


def _metric(label: str, value, fmt: str = ".4f") -> None:
    if isinstance(value, float):
        print(f"  {label:<25s} {BOLD}{value:{fmt}}{RESET}")
    else:
        print(f"  {label:<25s} {BOLD}{value}{RESET}")


def _table_row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False))


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: scan
# ═════════════════════════════════════════════════════════════════════════════

def cmd_scan(args: argparse.Namespace) -> None:
    """Scan one receipt, show results, let user correct, save as training data."""
    from app.training.real_world_trainer import real_world_trainer

    image_path = args.image
    _header("Receipt Scan & Label")
    print(f"  Image: {image_path}")

    # Scan
    print(f"\n{DIM}  Scanning...{RESET}", end="", flush=True)
    result = real_world_trainer.scan_receipt(image_path)
    print(f"\r{GREEN}  ✔ Scanned in {result['processing_ms']}ms{RESET}")

    items = result["parsed"]["items"]
    total = result["parsed"]["total_items"]
    confidence = result["parsed"]["avg_confidence"]

    _section(f"OCR Detected {len(items)} items (total qty: {total})")
    print(f"  {'#':<4s} {'Code':<15s} {'Qty':<8s} {'Confidence':<12s}")
    print(f"  {'─' * 4} {'─' * 15} {'─' * 8} {'─' * 12}")
    for i, item in enumerate(items, 1):
        code = item.get("code", "???")
        qty = item.get("quantity", 0)
        conf = item.get("confidence", 0.0)
        colour = GREEN if conf > 0.7 else YELLOW if conf > 0.4 else RED
        print(f"  {i:<4d} {code:<15s} {qty:<8.0f} {colour}{conf:.2f}{RESET}")

    print(f"\n  Average confidence: {confidence:.2f}")

    # Interactive correction
    _section("Review & Correct")
    print("  Enter corrections below. Press Enter to keep as-is.")
    print(f"  {DIM}Format: code,quantity  (e.g., TEW1,3){RESET}")
    print(f"  {DIM}Type 'add code,qty' to add a missed item{RESET}")
    print(f"  {DIM}Type 'del N' to delete item N{RESET}")
    print(f"  {DIM}Type 'done' when finished{RESET}\n")

    corrected = [dict(item) for item in items]  # deep copy
    to_delete = set()

    while True:
        try:
            user_input = input(f"  {CYAN}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input or user_input.lower() == "done":
            break

        if user_input.lower().startswith("del "):
            try:
                idx = int(user_input.split()[1]) - 1
                if 0 <= idx < len(corrected):
                    to_delete.add(idx)
                    _info(f"Marked item {idx + 1} for deletion")
                else:
                    _warning(f"Invalid item number: {idx + 1}")
            except (ValueError, IndexError):
                _warning("Usage: del <item_number>")
            continue

        if user_input.lower().startswith("add "):
            try:
                parts = user_input[4:].split(",")
                code = parts[0].strip().upper()
                qty = float(parts[1].strip()) if len(parts) > 1 else 1
                corrected.append({"code": code, "quantity": qty})
                _success(f"Added: {code} × {qty:.0f}")
            except (ValueError, IndexError):
                _warning("Usage: add code,quantity")
            continue

        # Edit item: "N code,qty" or just "N code"
        parts = user_input.split(None, 1)
        if len(parts) >= 2 and parts[0].isdigit():
            idx = int(parts[0]) - 1
            if 0 <= idx < len(corrected):
                edit_parts = parts[1].split(",")
                code = edit_parts[0].strip().upper()
                qty = (
                    float(edit_parts[1].strip())
                    if len(edit_parts) > 1
                    else corrected[idx].get("quantity", 1)
                )
                corrected[idx]["code"] = code
                corrected[idx]["quantity"] = qty
                _success(f"Item {idx + 1}: {code} × {qty:.0f}")
            else:
                _warning(f"Invalid item number: {idx + 1}")
        else:
            _warning("Format: <item_number> <code>,<qty> or 'add <code>,<qty>'")

    # Remove deleted items
    if to_delete:
        corrected = [
            item for i, item in enumerate(corrected) if i not in to_delete
        ]

    # Save
    _section("Saving Training Sample")
    receipt_type = input(
        f"  Receipt type [{CYAN}handwritten{RESET}/printed/mixed]: "
    ).strip() or "handwritten"
    notes = input("  Notes (optional): ").strip()

    sample = real_world_trainer.save_corrected_sample(
        image_path=image_path,
        corrected_items=corrected,
        receipt_id=result.get("receipt_id"),
        receipt_type=receipt_type,
        notes=notes,
        original_items=items,
    )

    _success(f"Sample saved: {sample.get('receipt_id')}")
    _info(f"  Items: {len(corrected)}, Total qty: {sum(i['quantity'] for i in corrected):.0f}")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: batch-scan
# ═════════════════════════════════════════════════════════════════════════════

def cmd_batch_scan(args: argparse.Namespace) -> None:
    """Batch-scan a folder of receipts and review each."""
    from app.training.real_world_trainer import real_world_trainer

    folder = args.folder
    _header(f"Batch Scan: {folder}")

    results = real_world_trainer.batch_scan(folder)
    if not results:
        _warning("No images found in folder.")
        return

    _success(f"Scanned {len(results)} receipts")
    errors = [r for r in results if "error" in r]
    if errors:
        _warning(f"{len(errors)} failed scans")

    _section("Summary")
    print(f"  {'#':<4s} {'Receipt':<30s} {'Items':<8s} {'Total Qty':<10s} {'Time (ms)':<10s}")
    print(f"  {'─' * 4} {'─' * 30} {'─' * 8} {'─' * 10} {'─' * 10}")
    for r in results:
        if "error" in r:
            name = Path(r["image_path"]).name
            print(f"  {r['scan_index']:<4d} {name:<30s} {RED}ERROR{RESET}")
            continue
        name = Path(r["image_path"]).name[:30]
        items_count = len(r["parsed"]["items"])
        total_qty = r["parsed"]["total_items"]
        ms = r["processing_ms"]
        print(f"  {r['scan_index']:<4d} {name:<30s} {items_count:<8d} {total_qty:<10d} {ms:<10d}")

    # Ask if user wants to save any as training data
    print(f"\n  {DIM}Use `trainer scan <image>` to interactively label individual receipts.{RESET}")

    if args.auto_save:
        _section("Auto-saving as training data (OCR output as-is)")
        saved = 0
        for r in results:
            if "error" in r:
                continue
            items = r["parsed"]["items"]
            if not items:
                continue
            try:
                real_world_trainer.save_corrected_sample(
                    image_path=r["image_path"],
                    corrected_items=items,
                    receipt_id=r.get("receipt_id"),
                    receipt_type="handwritten",
                    notes="Auto-saved from batch scan (uncorrected OCR)",
                )
                saved += 1
            except Exception as e:
                _error(f"Failed to save {r.get('receipt_id')}: {e}")
        _success(f"Saved {saved} training samples")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: analyze
# ═════════════════════════════════════════════════════════════════════════════

def cmd_analyze(args: argparse.Namespace) -> None:
    """Mine OCR error patterns from benchmark results and corrections."""
    from app.training.real_world_trainer import real_world_trainer

    _header("OCR Error Pattern Analysis")
    print(f"  {DIM}Analysing benchmark results and correction history...{RESET}")

    patterns = real_world_trainer.mine_error_patterns(verbose=True)

    total = patterns.get("total_errors_analysed", 0)
    top = patterns.get("top_error_patterns", [])

    if total == 0:
        _warning(
            "No error patterns found. Run benchmarks first:\n"
            "    python -m scripts.train benchmark"
        )
        return

    _success(f"Analysed {total} error instances")

    # Code-level patterns
    _section(f"Top Code-Level Confusions ({len(top)} found)")
    if top:
        print(f"  {'Misread':<15s} {'Correct':<15s} {'Count':<8s} {'Edit Dist':<10s}")
        print(f"  {'─' * 15} {'─' * 15} {'─' * 8} {'─' * 10}")
        for p in top[:20]:
            print(
                f"  {p['misread']:<15s} {p['correct']:<15s} "
                f"{p['occurrences']:<8d} {p['edit_distance']:<10d}"
            )

    # Character-level
    char_conf = patterns.get("char_confusions", {})
    if char_conf:
        _section("Character-Level Confusions")
        flat = []
        for ch_from, targets in char_conf.items():
            for ch_to, count in targets.items():
                flat.append((ch_from, ch_to, count))
        flat.sort(key=lambda x: x[2], reverse=True)

        print(f"  {'From':<8s} {'To':<8s} {'Count':<8s}")
        print(f"  {'─' * 8} {'─' * 8} {'─' * 8}")
        for ch_from, ch_to, count in flat[:15]:
            print(f"  '{ch_from}'  →   '{ch_to}'     {count}")

    _info("\nResults saved to: training_data/error_patterns.json")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: learn
# ═════════════════════════════════════════════════════════════════════════════

def cmd_learn(args: argparse.Namespace) -> None:
    """Generate learned OCR substitution rules from mined patterns."""
    from app.training.real_world_trainer import real_world_trainer

    _header("Generate Learned OCR Rules")
    min_occ = args.min_occurrences

    print(f"  Min occurrences threshold: {min_occ}")
    print(f"  {DIM}Mining patterns and generating rules...{RESET}")

    rules = real_world_trainer.generate_learned_rules(min_occurrences=min_occ)

    total = rules.get("rules_generated", 0)
    if total == 0:
        _warning(
            "No rules generated. Need more training data or lower threshold.\n"
            "  Add training samples with: trainer scan <image>\n"
            "  Run benchmarks with: python -m scripts.train benchmark"
        )
        return

    _success(f"Generated {total} learned rules")

    # Show char rules
    char_rules = rules.get("ocr_char_rules", {})
    if char_rules:
        _section(f"OCR Character Rules ({len(char_rules)})")
        for ch_from, ch_to in sorted(char_rules.items()):
            print(f"    '{ch_from}' → '{ch_to}'")

    # Show reverse rules
    rev_rules = rules.get("reverse_rules", {})
    if rev_rules:
        _section(f"Reverse OCR Rules ({len(rev_rules)})")
        for ch_from, ch_to in sorted(rev_rules.items()):
            print(f"    '{ch_from}' → '{ch_to}'")

    # Show code corrections
    code_corr = rules.get("code_corrections", {})
    if code_corr:
        _section(f"Code-Level Corrections ({len(code_corr)})")
        for misread, correct in sorted(code_corr.items()):
            print(f"    '{misread}' → '{correct}'")

    _info("\nRules saved to: training_data/learned_rules.json")
    _info("These rules will be auto-loaded by the parser on next scan.")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: confusion
# ═════════════════════════════════════════════════════════════════════════════

def cmd_confusion(args: argparse.Namespace) -> None:
    """Display the character-level confusion matrix."""
    from app.training.real_world_trainer import real_world_trainer

    _header("Character Confusion Matrix")

    matrix = real_world_trainer.build_confusion_matrix()
    total = matrix.get("total_confusions", 0)

    if total == 0:
        _warning("No confusion data. Run benchmarks and analysis first.")
        return

    _success(f"Total confusions tracked: {total}")

    ranked = matrix.get("most_confused", [])
    if ranked:
        _section("Top Character Confusions")
        print(f"  {'Char':<8s} {'Confused As':<12s} {'Count':<8s} {'Bar'}")
        print(f"  {'─' * 8} {'─' * 12} {'─' * 8} {'─' * 30}")

        max_count = ranked[0][2] if ranked else 1
        for ch_from, ch_to, count in ranked[:25]:
            bar_len = int(count / max_count * 25)
            bar = "█" * bar_len
            print(f"  '{ch_from}'  →   '{ch_to}'       {count:<8d} {CYAN}{bar}{RESET}")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: auto-improve
# ═════════════════════════════════════════════════════════════════════════════

def cmd_auto_improve(args: argparse.Namespace) -> None:
    """Run the full automatic improvement cycle."""
    from app.training.real_world_trainer import real_world_trainer

    _header("Auto-Improvement Cycle")
    print(f"  {DIM}Pipeline: Benchmark → Mine Errors → Learn Rules → Re-Benchmark{RESET}\n")

    result = real_world_trainer.run_improvement_cycle(verbose=args.verbose)

    if "error" in result:
        _error(result["error"])
        return

    _section("Baseline Metrics")
    for key, val in result.get("baseline", {}).items():
        _metric(key, val)

    _section("After Learning")
    for key, val in result.get("improved", {}).items():
        _metric(key, val)

    _section("Improvement")
    improvement = result.get("improvement", {})
    for key, val in improvement.items():
        if key.endswith("_delta"):
            label = key.replace("_delta", "")
            colour = GREEN if val > 0 else RED if val < 0 else ""
            print(f"  {label:<25s} {colour}{val:+.4f}{RESET}")

    _section("Summary")
    _metric("Error patterns found", result.get("error_patterns_found", 0), "d")
    _metric("Rules generated", result.get("rules_generated", 0), "d")
    _metric("Session ID", result.get("session_id", ""))

    _success("Improvement cycle complete!")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: report
# ═════════════════════════════════════════════════════════════════════════════

def cmd_report(args: argparse.Namespace) -> None:
    """Display a comprehensive training progress report."""
    from app.training.real_world_trainer import real_world_trainer

    _header("Training Progress Report")

    report = real_world_trainer.generate_report()

    # Training data
    td = report.get("training_data", {})
    _section("Training Data")
    _metric("Total samples", td.get("total_samples", 0), "d")

    # Learned rules
    lr = report.get("learned_rules", {})
    _section("Learned Rules")
    _metric("Total rules", lr.get("total", 0), "d")
    _metric("Character rules", lr.get("char_rules", 0), "d")
    _metric("Reverse rules", lr.get("reverse_rules", 0), "d")
    _metric("Code corrections", lr.get("code_corrections", 0), "d")

    # Trend
    trend = report.get("trend", {})
    _section("Accuracy Trend")
    if "note" in trend:
        _info(trend["note"])
    else:
        _metric("First F1", trend.get("first_f1", 0))
        _metric("Latest F1", trend.get("latest_f1", 0))
        delta = trend.get("total_improvement", 0)
        colour = GREEN if delta > 0 else RED if delta < 0 else ""
        print(f"  {'Total improvement':<25s} {colour}{delta:+.4f}{RESET}")
        _metric("Sessions run", trend.get("sessions_run", 0), "d")

    # Top confusions
    confusions = report.get("top_confusions", [])
    if confusions:
        _section("Top Character Confusions")
        for item in confusions[:8]:
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                print(f"    '{item[0]}' → '{item[1]}' ({item[2]}×)")

    # Sessions
    sessions = report.get("sessions", [])
    if sessions:
        _section(f"Recent Sessions ({len(sessions)})")
        for s in sessions[-5:]:
            sid = s.get("session_id", "?")
            base_f1 = s.get("baseline", {}).get("f1_score", 0)
            imp_f1 = s.get("improved", s.get("baseline", {})).get("f1_score", 0)
            delta = imp_f1 - base_f1
            colour = GREEN if delta > 0 else RED if delta < 0 else DIM
            print(
                f"    {sid}  F1: {base_f1:.4f} → {imp_f1:.4f}  "
                f"{colour}({delta:+.4f}){RESET}"
            )

    # Recommendations
    recs = report.get("recommendations", [])
    if recs:
        _section("Recommendations")
        for r in recs:
            print(f"    {r}")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: augment
# ═════════════════════════════════════════════════════════════════════════════

def cmd_augment(args: argparse.Namespace) -> None:
    """Generate augmented copies of training images."""
    from app.training.real_world_trainer import real_world_trainer

    _header("Image Augmentation")
    variations = args.variations
    source = args.source

    print(f"  Variations per image: {variations}")
    if source:
        print(f"  Source directory: {source}")

    result = real_world_trainer.augment_images(
        source_dir=source, variations=variations
    )

    if "error" in result:
        _error(result["error"])
        return

    _success(
        f"Generated {result['augmented_count']} augmented images "
        f"from {result['source_images']} originals"
    )
    _info(f"Output directory: {result['output_dir']}")


# ═════════════════════════════════════════════════════════════════════════════
#  COMMAND: status
# ═════════════════════════════════════════════════════════════════════════════

def cmd_status(args: argparse.Namespace) -> None:
    """Show a quick status overview."""
    from app.training.data_manager import TrainingDataManager
    from app.training.real_world_trainer import (
        CONFUSION_MATRIX_PATH,
        ERROR_PATTERNS_PATH,
        LEARNED_RULES_PATH,
        SESSION_HISTORY_PATH,
    )

    _header("Trainer Status")

    dm = TrainingDataManager()
    samples = dm.list_samples()
    _metric("Training samples", len(samples), "d")

    # Profiles
    profiles = dm.list_profiles()
    _metric("Saved profiles", len(profiles), "d")

    # Learned rules
    if LEARNED_RULES_PATH.exists():
        rules = json.loads(LEARNED_RULES_PATH.read_text(encoding="utf-8"))
        _metric("Learned rules", rules.get("rules_generated", 0), "d")
        _info(f"  Generated: {rules.get('generated_at', '?')}")
    else:
        _metric("Learned rules", "none")

    # Error patterns
    if ERROR_PATTERNS_PATH.exists():
        patterns = json.loads(ERROR_PATTERNS_PATH.read_text(encoding="utf-8"))
        _metric("Error patterns", patterns.get("total_errors_analysed", 0), "d")
    else:
        _metric("Error patterns", "none")

    # Confusion matrix
    if CONFUSION_MATRIX_PATH.exists():
        matrix = json.loads(CONFUSION_MATRIX_PATH.read_text(encoding="utf-8"))
        _metric("Char confusions", matrix.get("total_confusions", 0), "d")
    else:
        _metric("Char confusions", "none")

    # Sessions
    if SESSION_HISTORY_PATH.exists():
        sessions = json.loads(SESSION_HISTORY_PATH.read_text(encoding="utf-8"))
        _metric("Training sessions", len(sessions), "d")
        if sessions:
            latest = sessions[-1]
            _info(f"  Latest: {latest.get('session_id', '?')}")
    else:
        _metric("Training sessions", 0, "d")

    # Augmented images
    from app.training.real_world_trainer import AUGMENTED_DIR
    if AUGMENTED_DIR.exists():
        aug_count = len([
            f for f in AUGMENTED_DIR.iterdir()
            if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
        ])
        _metric("Augmented images", aug_count, "d")
    else:
        _metric("Augmented images", 0, "d")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN — argparse
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="trainer",
        description=(
            "Real-World Receipt Trainer — Interactive OCR training utility.\n"
            "Scan real receipts, correct OCR errors, mine patterns, and\n"
            "auto-generate substitution rules to improve accuracy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Workflow:
              1. Scan receipts:    trainer scan <image>
              2. Build dataset:    trainer batch-scan <folder>
              3. Mine patterns:    trainer analyze
              4. Generate rules:   trainer learn
              5. Full cycle:       trainer auto-improve
              6. Track progress:   trainer report
        """),
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    p_scan = sub.add_parser(
        "scan",
        help="Scan a receipt image, review OCR output, and correct interactively",
    )
    p_scan.add_argument("image", help="Path to receipt image file")
    p_scan.set_defaults(func=cmd_scan)

    # batch-scan
    p_batch = sub.add_parser(
        "batch-scan",
        help="Batch-scan a folder of receipt images",
    )
    p_batch.add_argument("folder", help="Path to folder containing receipt images")
    p_batch.add_argument(
        "--auto-save",
        action="store_true",
        help="Auto-save scanned results as training data (without correction)",
    )
    p_batch.set_defaults(func=cmd_batch_scan)

    # analyze
    p_analyze = sub.add_parser(
        "analyze",
        help="Mine OCR error patterns from benchmark results and corrections",
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # learn
    p_learn = sub.add_parser(
        "learn",
        help="Generate learned OCR substitution rules from mined patterns",
    )
    p_learn.add_argument(
        "--min-occurrences",
        type=int,
        default=2,
        help="Minimum times a pattern must appear to become a rule (default: 2)",
    )
    p_learn.set_defaults(func=cmd_learn)

    # confusion
    p_confusion = sub.add_parser(
        "confusion",
        help="Display the character-level confusion matrix",
    )
    p_confusion.set_defaults(func=cmd_confusion)

    # auto-improve
    p_auto = sub.add_parser(
        "auto-improve",
        help="Run the full auto-improvement cycle",
    )
    p_auto.add_argument("--verbose", action="store_true", help="Show per-image detail")
    p_auto.set_defaults(func=cmd_auto_improve)

    # report
    p_report = sub.add_parser(
        "report",
        help="Generate and display a training progress report",
    )
    p_report.set_defaults(func=cmd_report)

    # augment
    p_augment = sub.add_parser(
        "augment",
        help="Generate augmented copies of training images",
    )
    p_augment.add_argument(
        "--variations",
        type=int,
        default=3,
        help="Number of augmented copies per image (default: 3)",
    )
    p_augment.add_argument(
        "--source",
        default=None,
        help="Source directory of images (default: training_data/images)",
    )
    p_augment.set_defaults(func=cmd_augment)

    # status
    p_status = sub.add_parser(
        "status",
        help="Show a quick trainer status overview",
    )
    p_status.set_defaults(func=cmd_status)

    # ── Parse & dispatch ──
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
