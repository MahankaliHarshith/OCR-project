"""
Training CLI Script.

Command-line interface for the receipt scanner training pipeline.

Usage:
    python scripts/train.py add <image_path> --items '{"items": [{"code": "ABC", "quantity": 2}]}'
    python scripts/train.py list
    python scripts/train.py benchmark [--verbose]
    python scripts/train.py optimize [--strategy smart|grid] [--metric f1_score]
    python scripts/train.py learn-template [--id default]
    python scripts/train.py apply [--profile optimized]
    python scripts/train.py status
    python scripts/train.py add-folder <folder_path> --labels <labels.json>
"""

import argparse
import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def cmd_add(args):
    """Add a single training sample."""
    from app.training.data_manager import training_data_manager

    gt = json.loads(args.items)
    sample = training_data_manager.add_sample(
        image_path=args.image,
        ground_truth=gt,
        receipt_id=args.id,
    )
    print(f"✅ Added: {sample['receipt_id']}")
    print(f"   Items: {len(sample['items'])}")
    print(f"   Total qty: {sample['total_quantity']}")


def cmd_add_folder(args):
    """Add all images in a folder with a labels JSON file."""
    from pathlib import Path

    from app.training.data_manager import training_data_manager

    folder = Path(args.folder)
    if not folder.exists():
        print(f"❌ Folder not found: {folder}")
        return

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))

    added = 0
    for entry in labels:
        image_file = entry.get("image_file", "")
        image_path = folder / image_file

        if not image_path.exists():
            print(f"  ⚠️  Image not found: {image_file}")
            continue

        try:
            sample = training_data_manager.add_sample(
                image_path=str(image_path),
                ground_truth=entry,
                receipt_id=entry.get("receipt_id"),
            )
            print(f"  ✅ {sample['receipt_id']} ({len(sample['items'])} items)")
            added += 1
        except ValueError as e:
            print(f"  ❌ {image_file}: {e}")

    print(f"\n{'='*40}")
    print(f"Added {added}/{len(labels)} samples")


def cmd_list(args):
    """List all training samples."""
    from app.training.data_manager import training_data_manager

    samples = training_data_manager.list_samples()
    if not samples:
        print("No training samples found.")
        print("Add samples with: python scripts/train.py add <image> --items '{...}'")
        return

    print(f"\n{'='*60}")
    print(f"  TRAINING SAMPLES ({len(samples)} total)")
    print(f"{'='*60}")
    for s in samples:
        status = "✅" if s.get("image_exists") else "❌"
        print(
            f"  {status} {s['receipt_id']:<20} "
            f"items={len(s['items']):<3} "
            f"total_qty={s.get('total_quantity', '?'):<5} "
            f"type={s.get('receipt_type', '?')}"
        )
    print()


def cmd_benchmark(args):
    """Run accuracy benchmark."""
    from app.training.benchmark import benchmark_engine
    from app.training.data_manager import training_data_manager

    samples = training_data_manager.get_sample_pairs()
    if not samples:
        print("❌ No training samples. Add some first.")
        return

    print(f"\nBenchmarking {len(samples)} samples...")
    print("=" * 60)

    result = benchmark_engine.run_benchmark(samples, verbose=args.verbose)

    # Save result
    path = training_data_manager.save_benchmark_result(result)

    metrics = result["aggregate_metrics"]
    print(f"\n{'='*60}")
    print("  BENCHMARK RESULTS")
    print(f"{'='*60}")
    print(f"  Samples:           {result['total_samples']}")
    print(f"  Total time:        {result['total_elapsed_s']:.1f}s")
    print(f"  Avg per receipt:   {result['avg_time_per_receipt_ms']:.0f}ms")
    print(f"{'─'*60}")
    print(f"  Precision:         {metrics['precision']:.4f}")
    print(f"  Recall:            {metrics['recall']:.4f}")
    print(f"  F1 Score:          {metrics['f1_score']:.4f}")
    print(f"  Code Accuracy:     {metrics['code_accuracy']:.4f}")
    print(f"  Qty Accuracy:      {metrics['qty_accuracy']:.4f}")
    print(f"  Total Qty Acc:     {metrics['total_qty_accuracy']:.4f}")
    print(f"  Avg Confidence:    {metrics['avg_confidence']:.4f}")
    print(f"{'='*60}")

    if args.verbose and "per_image" in result:
        print("\nPER-IMAGE DETAILS:")
        for img in result["per_image"]:
            status = "✅" if img["f1"] >= 0.8 else "⚠️" if img["f1"] >= 0.5 else "❌"
            print(
                f"  {status} {img['receipt_id']:<20} "
                f"F1={img['f1']:.3f} "
                f"P={img['precision']:.3f} "
                f"R={img['recall']:.3f} "
                f"({img['processing_ms']}ms)"
            )
            if img.get("missing_codes"):
                print(f"      Missing: {', '.join(img['missing_codes'])}")
            if img.get("extra_codes"):
                print(f"      Extra:   {', '.join(img['extra_codes'])}")
            if img.get("qty_mismatches"):
                for m in img["qty_mismatches"]:
                    print(f"      Qty mismatch: {m['code']} expected={m['expected']} got={m['detected']}")

    print(f"\nSaved to: {path}")


def cmd_optimize(args):
    """Run parameter optimization."""
    from app.training.data_manager import training_data_manager
    from app.training.optimizer import DEFAULT_SEARCH_SPACE, QUICK_SEARCH_SPACE, optimizer

    samples = training_data_manager.get_sample_pairs()
    if len(samples) < 2:
        print(f"❌ Need ≥ 2 training samples (have {len(samples)}). Add more first.")
        return

    space = QUICK_SEARCH_SPACE if args.quick else DEFAULT_SEARCH_SPACE
    combos = 1
    for v in space.values():
        combos *= len(v)

    print(f"\n{'='*60}")
    print("  AUTO-TUNING OCR PARAMETERS")
    print(f"{'='*60}")
    print(f"  Strategy:     {args.strategy}")
    print(f"  Metric:       {args.metric}")
    print(f"  Samples:      {len(samples)}")
    print(f"  Search space: {combos} combinations")
    print(f"{'='*60}")
    print()

    if args.strategy == "grid":
        result = optimizer.grid_search(
            samples, search_space=space, metric=args.metric, verbose=True
        )
    else:
        result = optimizer.smart_tune(
            samples,
            search_space=space,
            metric=args.metric,
            max_rounds=args.max_rounds,
        )

    print(f"\n{'='*60}")
    print("  OPTIMIZATION RESULTS")
    print(f"{'='*60}")
    print(f"  Baseline {args.metric}:  {result.get('baseline_score', result.get('best_score', 0)):.4f}")
    print(f"  Optimized {args.metric}: {result.get('optimized_score', result.get('best_score', 0)):.4f}")
    improvement = result.get("improvement", 0)
    print(f"  Improvement:       {'+' if improvement >= 0 else ''}{improvement:.4f}")
    print()
    print("  Best parameters:")
    for k, v in result.get("best_params", {}).items():
        print(f"    {k:<20} = {v}")

    # Save profile
    profile = {
        "params": result.get("best_params", {}),
        "metrics": result.get("optimized_metrics", {}),
        "strategy": args.strategy,
    }
    path = training_data_manager.save_profile(profile, "optimized")
    training_data_manager.save_benchmark_result(result)
    print(f"\n  Profile saved: {path}")
    print("  Apply with: python scripts/train.py apply")


def cmd_apply(args):
    """Apply an optimized profile."""
    from app.training.data_manager import training_data_manager
    from app.training.optimizer import optimizer

    profile = training_data_manager.load_profile(args.profile)
    if not profile:
        print(f"❌ Profile '{args.profile}' not found. Run optimize first.")
        return

    changes = optimizer.apply_profile(profile.get("params", profile))

    print(f"\n{'='*60}")
    print(f"  PROFILE APPLIED: {args.profile}")
    print(f"{'='*60}")
    if changes:
        for key, val in changes.items():
            print(f"  {key}: {val['old']} → {val['new']}")
    else:
        print("  No changes needed (already optimal).")


def cmd_learn_template(args):
    """Learn a receipt template."""
    from app.training.data_manager import training_data_manager
    from app.training.template_learner import template_learner

    samples = training_data_manager.get_sample_pairs()
    if not samples:
        print("❌ No training samples. Add some first.")
        return

    print(f"Learning template '{args.id}' from {len(samples)} samples...")
    template = template_learner.learn_template(samples, args.id)
    path = template_learner.save_template(template)

    info = template.to_dict()
    print(f"\n{'='*60}")
    print(f"  TEMPLATE: {args.id}")
    print(f"{'='*60}")
    print(f"  Samples used:      {info['samples_used']}")
    print(f"  Structured:        {info['characteristics']['is_structured']}")
    print(f"  Has prices:        {info['characteristics']['has_prices']}")
    print(f"  Has line numbers:  {info['characteristics']['has_line_numbers']}")
    print(f"  Code column X:     {info['layout']['code_column_x']:.3f}")
    print(f"  Qty column X:      {info['layout']['qty_column_x']:.3f}")
    print(f"  Item region Y:     {info['layout']['item_start_y']:.3f} - {info['layout']['item_end_y']:.3f}")
    print(f"  Avg line height:   {info['layout']['avg_line_height']:.4f}")
    print(f"\n  Saved to: {path}")


def cmd_status(args):
    """Show training pipeline status."""
    from app.training.data_manager import training_data_manager
    from app.training.optimizer import optimizer
    from app.training.template_learner import template_learner

    samples = training_data_manager.count_samples()
    profiles = training_data_manager.list_profiles()
    templates = template_learner.list_templates()
    results = training_data_manager.list_benchmark_results()
    params = optimizer.get_current_params()

    print(f"\n{'='*60}")
    print("  TRAINING PIPELINE STATUS")
    print(f"{'='*60}")
    print(f"  Training samples:  {samples}")
    print(f"  Saved profiles:    {', '.join(profiles) if profiles else 'None'}")
    print(f"  Templates:         {', '.join(templates) if templates else 'None'}")
    print(f"  Benchmark runs:    {len(results)}")
    print()
    print("  Current OCR parameters:")
    for k, v in params.items():
        print(f"    {k:<20} = {v}")
    print()

    if results:
        latest = results[0]
        m = latest.get("aggregate_metrics", {})
        print("  Latest benchmark:")
        print(f"    F1:       {m.get('f1_score', 'N/A')}")
        print(f"    Prec:     {m.get('precision', 'N/A')}")
        print(f"    Recall:   {m.get('recall', 'N/A')}")
        print(f"    Code Acc: {m.get('code_accuracy', 'N/A')}")


def main():
    parser = argparse.ArgumentParser(
        description="OCR Receipt Scanner — Training Pipeline CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Add a training sample
  python scripts/train.py add receipt.jpg --items '{"items": [{"code": "ABC", "quantity": 2}, {"code": "DEF", "quantity": 3}]}'

  # Add multiple from a folder
  python scripts/train.py add-folder ./receipts --labels labels.json

  # List all samples
  python scripts/train.py list

  # Run benchmark
  python scripts/train.py benchmark --verbose

  # Auto-tune parameters
  python scripts/train.py optimize --strategy smart --metric f1_score

  # Apply optimized profile
  python scripts/train.py apply

  # Learn receipt template
  python scripts/train.py learn-template --id handwritten

  # Check status
  python scripts/train.py status
""",
    )

    sub = parser.add_subparsers(dest="command", help="Training command")

    # add
    p_add = sub.add_parser("add", help="Add a labeled receipt image")
    p_add.add_argument("image", help="Path to receipt image")
    p_add.add_argument("--items", required=True, help="Ground truth JSON string")
    p_add.add_argument("--id", default=None, help="Custom receipt ID")

    # add-folder
    p_folder = sub.add_parser("add-folder", help="Add images from a folder")
    p_folder.add_argument("folder", help="Folder containing receipt images")
    p_folder.add_argument("--labels", required=True, help="Path to labels JSON file")

    # list
    sub.add_parser("list", help="List all training samples")

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Run accuracy benchmark")
    p_bench.add_argument("--verbose", "-v", action="store_true")

    # optimize
    p_opt = sub.add_parser("optimize", help="Auto-tune OCR parameters")
    p_opt.add_argument("--strategy", default="smart", choices=["smart", "grid"])
    p_opt.add_argument("--metric", default="f1_score")
    p_opt.add_argument("--max-rounds", type=int, default=3)
    p_opt.add_argument("--quick", action="store_true", default=True)
    p_opt.add_argument("--full", action="store_true", dest="full_space")

    # apply
    p_apply = sub.add_parser("apply", help="Apply optimized profile")
    p_apply.add_argument("--profile", default="optimized")

    # learn-template
    p_tmpl = sub.add_parser("learn-template", help="Learn receipt template")
    p_tmpl.add_argument("--id", default="default", help="Template name")

    # status
    sub.add_parser("status", help="Show training pipeline status")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    commands = {
        "add": cmd_add,
        "add-folder": cmd_add_folder,
        "list": cmd_list,
        "benchmark": cmd_benchmark,
        "optimize": cmd_optimize,
        "apply": cmd_apply,
        "learn-template": cmd_learn_template,
        "status": cmd_status,
    }

    # Handle --full flag for optimize
    if args.command == "optimize" and hasattr(args, "full_space") and args.full_space:
        args.quick = False

    commands[args.command](args)


if __name__ == "__main__":
    main()
