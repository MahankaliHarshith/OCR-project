"""
Training API Routes.

REST API endpoints for the training pipeline:
    POST   /api/training/upload      — Upload labeled receipt for training
    GET    /api/training/samples     — List all training samples
    GET    /api/training/samples/:id — Get a single training sample
    DELETE /api/training/samples/:id — Delete a training sample
    POST   /api/training/benchmark   — Run accuracy benchmark
    POST   /api/training/optimize    — Auto-tune OCR parameters
    POST   /api/training/learn       — Learn receipt template
    GET    /api/training/status      — Training pipeline status
    GET    /api/training/profiles    — List saved profiles
    POST   /api/training/apply       — Apply optimized profile
    GET    /api/training/params      — Get current OCR parameters
"""

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List as TypingList

from app.training.data_manager import training_data_manager, ALLOWED_EXTENSIONS
from app.training.benchmark import benchmark_engine
from app.training.optimizer import optimizer
from app.training.template_learner import template_learner

logger = logging.getLogger(__name__)
training_router = APIRouter(prefix="/api/training", tags=["Training"])


# ─── Request Models ──────────────────────────────────────────────────────────

class GroundTruthItem(BaseModel):
    code: str = Field(..., min_length=1, max_length=10, description="Product code")
    quantity: float = Field(..., gt=0, description="Expected quantity")


class GroundTruthPayload(BaseModel):
    items: TypingList[GroundTruthItem]
    total_quantity: Optional[float] = None
    receipt_type: Optional[str] = "unknown"
    notes: Optional[str] = ""


class OptimizeRequest(BaseModel):
    strategy: str = Field(default="smart", description="'smart' or 'grid'")
    metric: str = Field(default="f1_score", description="Metric to optimize")
    max_rounds: int = Field(default=3, ge=1, le=10)
    quick: bool = Field(default=True, description="Use reduced search space")


class ApplyProfileRequest(BaseModel):
    profile_name: str = Field(default="optimized")


# ─── Upload Training Data ───────────────────────────────────────────────────

@training_router.post("/upload", summary="Upload a labeled receipt for training")
async def upload_training_sample(
    file: UploadFile = File(..., description="Receipt image file"),
    ground_truth: str = Form(
        ...,
        description='JSON string: {"items": [{"code": "ABC", "quantity": 2}]}',
    ),
    receipt_id: Optional[str] = Form(None, description="Custom receipt ID"),
):
    """
    Upload a receipt image with ground truth labels for training.

    The ground truth JSON must contain an 'items' array where each item
    has a 'code' (product code) and 'quantity' (expected quantity).
    """
    # Validate file type
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")

    # Parse ground truth JSON
    try:
        gt_data = json.loads(ground_truth)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid ground_truth JSON.")

    # Read image bytes
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:  # 20MB limit
        raise HTTPException(400, "File too large (>20MB).")

    try:
        sample = training_data_manager.add_sample_from_bytes(
            image_bytes=contents,
            filename=file.filename or "receipt.jpg",
            ground_truth=gt_data,
            receipt_id=receipt_id,
        )
        return JSONResponse({
            "status": "success",
            "message": f"Training sample '{sample['receipt_id']}' added.",
            "sample": sample,
        })
    except ValueError as e:
        raise HTTPException(400, str(e))


@training_router.post("/upload-batch", summary="Upload ground truth for existing images")
async def upload_ground_truth_batch(
    payload: TypingList[dict],
):
    """
    Add ground truth labels for images already in training_data/images/.

    Expects a JSON array:
    [
        {"image_file": "receipt_001.jpg", "items": [{"code": "ABC", "quantity": 2}]},
        ...
    ]
    """
    from app.training.data_manager import IMAGES_DIR

    added = []
    errors = []

    for entry in payload:
        image_file = entry.get("image_file", "")
        image_path = IMAGES_DIR / image_file

        if not image_path.exists():
            errors.append(f"Image not found: {image_file}")
            continue

        try:
            sample = training_data_manager.add_sample(
                image_path=str(image_path),
                ground_truth=entry,
                receipt_id=entry.get("receipt_id"),
                copy_image=False,  # Already in images dir
            )
            added.append(sample["receipt_id"])
        except ValueError as e:
            errors.append(f"{image_file}: {e}")

    return JSONResponse({
        "status": "success",
        "added": len(added),
        "errors": errors,
        "receipt_ids": added,
    })


# ─── Query Training Data ────────────────────────────────────────────────────

@training_router.get("/samples", summary="List training samples")
async def list_training_samples():
    """List all training samples with their ground truth labels."""
    samples = training_data_manager.list_samples()
    return {
        "total": len(samples),
        "samples": samples,
    }


@training_router.get("/samples/{receipt_id}", summary="Get a training sample")
async def get_training_sample(receipt_id: str):
    """Get a single training sample by ID."""
    sample = training_data_manager.get_sample(receipt_id)
    if not sample:
        raise HTTPException(404, f"Sample '{receipt_id}' not found.")
    return sample


@training_router.delete("/samples/{receipt_id}", summary="Delete a training sample")
async def delete_training_sample(receipt_id: str):
    """Delete a training sample (image + label)."""
    deleted = training_data_manager.delete_sample(receipt_id)
    if not deleted:
        raise HTTPException(404, f"Sample '{receipt_id}' not found.")
    return {"status": "deleted", "receipt_id": receipt_id}


# ─── Benchmark ───────────────────────────────────────────────────────────────

@training_router.post("/benchmark", summary="Run accuracy benchmark")
async def run_benchmark(
    verbose: bool = Query(False, description="Include per-image details"),
):
    """
    Run the OCR pipeline against all training samples and compute
    accuracy metrics (precision, recall, F1, code/qty accuracy).
    """
    samples = training_data_manager.get_sample_pairs()
    if not samples:
        raise HTTPException(
            400,
            "No training samples found. Upload receipts first via POST /api/training/upload.",
        )

    result = benchmark_engine.run_benchmark(samples, verbose=verbose)

    # Save result
    training_data_manager.save_benchmark_result(result)

    return result


# ─── Auto-Tune ───────────────────────────────────────────────────────────────

@training_router.post("/optimize", summary="Auto-tune OCR parameters")
async def run_optimization(request: OptimizeRequest):
    """
    Run parameter optimization against training data.

    Finds the best OCR parameter combination to maximize the chosen
    metric (default: F1 score).
    """
    samples = training_data_manager.get_sample_pairs()
    if not samples:
        raise HTTPException(
            400,
            "No training samples. Upload receipts first.",
        )

    if len(samples) < 2:
        raise HTTPException(
            400,
            "Need at least 2 training samples for optimization. "
            f"Currently have {len(samples)}.",
        )

    from app.training.optimizer import QUICK_SEARCH_SPACE, DEFAULT_SEARCH_SPACE

    space = QUICK_SEARCH_SPACE if request.quick else DEFAULT_SEARCH_SPACE

    if request.strategy == "grid":
        result = optimizer.grid_search(
            samples, search_space=space, metric=request.metric, verbose=True
        )
    else:
        result = optimizer.smart_tune(
            samples,
            search_space=space,
            metric=request.metric,
            max_rounds=request.max_rounds,
            verbose=True,
        )

    # Save optimized profile
    if result.get("best_params"):
        profile = {
            "params": result["best_params"],
            "metrics": result.get("optimized_metrics", result.get("best_score")),
            "strategy": request.strategy,
            "timestamp": result.get("timestamp"),
        }
        training_data_manager.save_profile(profile, "optimized")

    # Save benchmark result
    training_data_manager.save_benchmark_result(result)

    return result


@training_router.post("/apply", summary="Apply optimized parameters")
async def apply_profile(request: ApplyProfileRequest):
    """
    Apply a saved optimized parameter profile to the running config.
    """
    profile = training_data_manager.load_profile(request.profile_name)
    if not profile:
        raise HTTPException(
            404,
            f"Profile '{request.profile_name}' not found. Run optimization first.",
        )

    changes = optimizer.apply_profile(profile.get("params", profile))
    return {
        "status": "applied",
        "profile": request.profile_name,
        "changes": changes,
    }


# ─── Template Learning ──────────────────────────────────────────────────────

@training_router.post("/learn-template", summary="Learn receipt template")
async def learn_template(
    template_id: str = Query("default", description="Template name"),
):
    """
    Analyze training images to learn receipt layout patterns.

    The learned template captures column positions, spacing, and
    structure characteristics to speed up future processing.
    """
    samples = training_data_manager.get_sample_pairs()
    if not samples:
        raise HTTPException(400, "No training samples. Upload receipts first.")

    template = template_learner.learn_template(samples, template_id)
    template_learner.save_template(template)

    return {
        "status": "success",
        "template": template.to_dict(),
    }


# ─── Status & Config ────────────────────────────────────────────────────────

@training_router.get("/status", summary="Training pipeline status")
async def training_status():
    """Get current status of training data, profiles, and parameters."""
    return {
        "training_samples": training_data_manager.count_samples(),
        "available_profiles": training_data_manager.list_profiles(),
        "available_templates": template_learner.list_templates(),
        "benchmark_results": len(training_data_manager.list_benchmark_results()),
        "current_params": optimizer.get_current_params(),
    }


@training_router.get("/params", summary="Get current OCR parameters")
async def get_current_params():
    """Get the current OCR parameter values."""
    return optimizer.get_current_params()


@training_router.get("/profiles", summary="List saved profiles")
async def list_profiles():
    """List all saved optimization profiles."""
    profiles = []
    for name in training_data_manager.list_profiles():
        data = training_data_manager.load_profile(name)
        if data:
            profiles.append({"name": name, "data": data})
    return {"profiles": profiles}


@training_router.get("/results", summary="List benchmark results")
async def list_benchmark_results():
    """List all saved benchmark run results."""
    results = training_data_manager.list_benchmark_results()
    return {"total": len(results), "results": results}
