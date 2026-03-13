"""
FastAPI API Routes.
Defines all REST API endpoints for the Receipt Scanner application.
"""

import os
import json
import logging
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

import numpy as np
import re

from fastapi import APIRouter, File, UploadFile, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import List as TypingList


class NumpyEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles numpy types."""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

from app.config import UPLOAD_DIR, EXPORT_DIR, ALLOWED_IMAGE_EXTENSIONS, MAX_FILE_SIZE_MB
from app.services.receipt_service import receipt_service
from app.services.product_service import product_service
from app.services.excel_service import excel_service
from app.ocr.hybrid_engine import get_hybrid_engine

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Health Check ─────────────────────────────────────────────────────────────

@router.get("/api/health", tags=["System"])
async def health_check():
    """
    Health check endpoint for monitoring and tunnel verification.
    Returns server status, uptime, and engine readiness.
    """
    import time as _time
    from app.ocr.hybrid_engine import get_hybrid_engine

    hybrid = get_hybrid_engine()
    return {
        "status": "healthy",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat(),
        "ocr_mode": hybrid.mode,
        "azure_available": hybrid._azure_ok,
        "local_loaded": hybrid._local_engine is not None,
    }


# ─── Request / Response Models ────────────────────────────────────────────────

class ProductCreate(BaseModel):
    product_code: str = Field(..., min_length=1, max_length=10)
    product_name: str = Field(..., min_length=1, max_length=200)
    category: str = Field(default="", max_length=50)
    unit: str = Field(default="Piece", max_length=20)

    @field_validator('product_code')
    @classmethod
    def sanitize_code(cls, v: str) -> str:
        """Allow only alphanumeric codes, dashes, and underscores."""
        v = v.strip().upper()
        if not re.match(r'^[A-Z0-9_\-]{1,10}$', v):
            raise ValueError('Product code must be 1-10 alphanumeric characters (A-Z, 0-9, -, _)')
        return v

    @field_validator('product_name')
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        """Strip dangerous characters from product names."""
        return re.sub(r'[<>{}\\]', '', v).strip()


class ProductUpdate(BaseModel):
    product_name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None

    @field_validator('product_name')
    @classmethod
    def sanitize_name(cls, v):
        if v is not None:
            return re.sub(r'[<>{}\\]', '', v).strip()
        return v


class ItemUpdate(BaseModel):
    product_code: str
    product_name: str
    quantity: float
    unit_price: float = 0.0
    line_total: float = 0.0

    @field_validator('product_code')
    @classmethod
    def sanitize_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.match(r'^[A-Z0-9_\-]{1,10}$', v):
            raise ValueError('Product code must be 1-10 alphanumeric characters')
        return v

    @field_validator('product_name')
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        return re.sub(r'[<>{}\\]', '', v).strip()

    @field_validator('quantity')
    @classmethod
    def validate_quantity(cls, v: float) -> float:
        if v <= 0 or v > 99999:
            raise ValueError('Quantity must be between 1 and 99999')
        return v

    @field_validator('unit_price', 'line_total')
    @classmethod
    def validate_price(cls, v: float) -> float:
        if v < 0 or v > 9999999:
            raise ValueError('Price values must be between 0 and 9,999,999')
        return round(v, 2)


class ExcelGenerateRequest(BaseModel):
    receipt_ids: list[int]


# ─── Receipt Processing Endpoints ────────────────────────────────────────────

@router.post("/api/receipts/scan", tags=["Receipts"])
async def scan_receipt(file: UploadFile = File(...)):
    """
    Upload and process a receipt image.

    Accepts JPEG, PNG, BMP, TIFF, or WebP images.
    Returns structured receipt data with OCR results.
    """
    # Validate file extension
    ext = Path(file.filename or "").suffix.lower()
    # Sanitize filename for logging to prevent log injection via newlines/control chars
    safe_log_name = (file.filename or "").replace("\n", "_").replace("\r", "_").replace("\t", "_")[:200]
    logger.info(f"Receipt scan request: filename={safe_log_name!r}, content_type={file.content_type}")
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. "
                   f"Allowed: {', '.join(ALLOWED_IMAGE_EXTENSIONS)}",
        )

    # Validate file size — stream-read in chunks to avoid OOM on huge files
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    contents = bytearray()
    chunk_size = 1024 * 1024  # Read 1MB at a time
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        contents.extend(chunk)
        if len(contents) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"File too large (>{MAX_FILE_SIZE_MB}MB). Maximum: {MAX_FILE_SIZE_MB}MB",
            )
    contents = bytes(contents)
    size_mb = len(contents) / (1024 * 1024)
    logger.debug(f"Upload file size: {size_mb:.2f} MB")

    # Validate file magic bytes (prevents disguised uploads)
    _MAGIC = {
        b'\xff\xd8\xff': '.jpg',       # JPEG
        b'\x89PNG': '.png',             # PNG
        b'BM': '.bmp',                  # BMP
        b'II\x2a\x00': '.tiff',        # TIFF (little-endian)
        b'MM\x00\x2a': '.tiff',        # TIFF (big-endian)
    }
    # WebP needs a two-part check: RIFF header + WEBP at offset 8
    is_webp = (len(contents) >= 12 and contents[:4] == b'RIFF' and contents[8:12] == b'WEBP')
    magic_ok = is_webp or any(contents[:len(sig)] == sig for sig in _MAGIC)
    if not magic_ok:
        raise HTTPException(
            status_code=400,
            detail="File content does not match a valid image format. "
                   "Upload a real JPEG, PNG, BMP, TIFF, or WebP image.",
        )

    # Save uploaded file (uuid suffix prevents collision on concurrent uploads)
    import uuid
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique = uuid.uuid4().hex[:6]
    filename = f"upload_{timestamp}_{unique}{ext}"
    upload_path = UPLOAD_DIR / filename

    with open(upload_path, "wb") as f:
        f.write(contents)

    # Process receipt (run in thread to avoid blocking the async event loop)
    try:
        import asyncio
        result = await asyncio.to_thread(receipt_service.process_receipt, str(upload_path))
        # Serialize with numpy-safe encoder, then parse back for JSONResponse
        safe_json = json.loads(json.dumps(result, cls=NumpyEncoder))
        logger.info(
            f"Receipt scan complete: success={result.get('success')}, "
            f"items={result.get('receipt_data', {}).get('total_items', 0) if result.get('receipt_data') else 0}"
        )
        return JSONResponse(content=safe_json)
    except Exception as e:
        logger.error(f"Receipt processing failed: {e}", exc_info=True)
        # Clean up orphaned upload file on failure
        try:
            upload_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Receipt processing failed. Please check the server logs.")


@router.post("/api/receipts/scan-batch", tags=["Receipts"])
async def scan_receipts_batch(files: TypingList[UploadFile] = File(...)):
    """
    Upload and process multiple receipt images in one request.

    Accepts up to 20 images per batch. Each image is validated and processed
    sequentially. Returns an array of results (one per file), each with
    success/failure status so the frontend can display per-file feedback.
    """
    MAX_BATCH_FILES = 20

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_BATCH_FILES} images per batch upload.",
        )
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No files provided.")

    import asyncio
    import uuid

    results = []
    for idx, file in enumerate(files):
        file_result = {"filename": file.filename, "index": idx, "success": False}

        try:
            # Validate extension
            ext = Path(file.filename or "").suffix.lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                file_result["error"] = f"Unsupported file type '{ext}'."
                results.append(file_result)
                continue

            # Read and validate size
            max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
            contents = bytearray()
            chunk_size = 1024 * 1024
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                contents.extend(chunk)
                if len(contents) > max_bytes:
                    file_result["error"] = f"File too large (>{MAX_FILE_SIZE_MB}MB)."
                    break
            if "error" in file_result:
                results.append(file_result)
                continue
            contents = bytes(contents)

            # Validate magic bytes
            _MAGIC = {
                b'\xff\xd8\xff': '.jpg',
                b'\x89PNG': '.png',
                b'BM': '.bmp',
                b'II\x2a\x00': '.tiff',
                b'MM\x00\x2a': '.tiff',
            }
            # WebP needs a two-part check: RIFF header + WEBP at offset 8
            is_webp = (len(contents) >= 12 and contents[:4] == b'RIFF' and contents[8:12] == b'WEBP')
            magic_ok = is_webp or any(contents[:len(sig)] == sig for sig in _MAGIC)
            if not magic_ok:
                file_result["error"] = "File content does not match a valid image format."
                results.append(file_result)
                continue

            # Save uploaded file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique = uuid.uuid4().hex[:6]
            filename = f"upload_{timestamp}_{unique}{ext}"
            upload_path = UPLOAD_DIR / filename

            with open(upload_path, "wb") as f:
                f.write(contents)

            # Process receipt
            result = await asyncio.to_thread(receipt_service.process_receipt, str(upload_path))
            safe_json = json.loads(json.dumps(result, cls=NumpyEncoder))
            file_result["success"] = safe_json.get("success", False)
            file_result["data"] = safe_json
            logger.info(
                f"Batch scan [{idx+1}/{len(files)}] complete: "
                f"filename={file.filename!r}, success={file_result['success']}"
            )
        except Exception as e:
            logger.error(f"Batch scan [{idx+1}/{len(files)}] failed: {e}", exc_info=True)
            file_result["error"] = "Processing failed for this file."
            # Clean up orphaned upload file on failure
            try:
                if 'upload_path' in dir() and upload_path and Path(upload_path).exists():
                    Path(upload_path).unlink(missing_ok=True)
            except OSError:
                pass

        results.append(file_result)

    total_success = sum(1 for r in results if r["success"])
    logger.info(f"Batch scan complete: {total_success}/{len(files)} succeeded")
    return JSONResponse(content={
        "batch_results": results,
        "total": len(files),
        "succeeded": total_success,
        "failed": len(files) - total_success,
    })


# ─── Async Batch Processing Endpoints ─────────────────────────────────────────

@router.post("/api/batch", tags=["Batch Processing"])
async def create_batch(files: TypingList[UploadFile] = File(...)):
    """
    Submit multiple receipt images for **asynchronous** background processing.

    Unlike `/api/receipts/scan-batch` (which blocks until all files are done),
    this endpoint returns immediately with a `batch_id` that you can poll via
    `GET /api/batch/{batch_id}`.

    - Max 20 files per batch, max 5 active batches at a time.
    - Each file is validated (extension, size, magic bytes) before queuing.
    """
    from app.services.batch_service import get_batch_service

    MAX_BATCH_FILES = 20
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_BATCH_FILES} images per async batch.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    import uuid as _uuid

    saved_paths: list[tuple[str, str]] = []  # (original_filename, disk_path)

    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}' in file '{file.filename}'. "
                       f"Allowed: {', '.join(ALLOWED_IMAGE_EXTENSIONS)}",
            )

        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        contents = bytearray()
        chunk_size = 1024 * 1024
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            contents.extend(chunk)
            if len(contents) > max_bytes:
                # Clean up already-saved files
                for _, p in saved_paths:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{file.filename}' too large (>{MAX_FILE_SIZE_MB}MB).",
                )
        contents = bytes(contents)

        # Validate magic bytes
        _MAGIC = {
            b'\xff\xd8\xff': '.jpg',
            b'\x89PNG': '.png',
            b'BM': '.bmp',
            b'II\x2a\x00': '.tiff',
            b'MM\x00\x2a': '.tiff',
        }
        # WebP needs a two-part check: RIFF header + WEBP at offset 8
        is_webp = (len(contents) >= 12 and contents[:4] == b'RIFF' and contents[8:12] == b'WEBP')
        if not is_webp and not any(contents[:len(sig)] == sig for sig in _MAGIC):
            for _, p in saved_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.filename}' content does not match a valid image format.",
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = _uuid.uuid4().hex[:6]
        disk_name = f"upload_{timestamp}_{unique}{ext}"
        upload_path = UPLOAD_DIR / disk_name
        with open(upload_path, "wb") as f:
            f.write(contents)

        saved_paths.append((file.filename or disk_name, str(upload_path)))

    # Create async batch job
    batch_svc = get_batch_service()
    try:
        batch = await batch_svc.create_batch(saved_paths)
    except ValueError as e:
        # Too many active batches
        for _, p in saved_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=429, detail=str(e))

    batch_id = batch.batch_id
    logger.info(f"Async batch created: batch_id={batch_id}, files={len(saved_paths)}")
    return JSONResponse(
        status_code=202,
        content={
            "batch_id": batch_id,
            "total_files": len(saved_paths),
            "status": "pending",
            "poll_url": f"/api/batch/{batch_id}",
            "ws_url": f"/ws/batch/{batch_id}",
        },
    )


@router.get("/api/batch", tags=["Batch Processing"])
async def list_batches(limit: int = Query(default=20, ge=1, le=100)):
    """List recent async batch jobs (newest first)."""
    from app.services.batch_service import get_batch_service
    batches = await get_batch_service().list_batches(limit)
    return {"batches": batches, "count": len(batches)}


@router.get("/api/batch/{batch_id}", tags=["Batch Processing"])
async def get_batch_status(batch_id: str):
    """
    Poll the status of an async batch job.

    Returns the batch status, progress percentage, and per-file results
    once processing is complete.
    """
    from app.services.batch_service import get_batch_service
    batch = await get_batch_service().get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return batch.to_dict(include_results=True)


@router.delete("/api/batch/{batch_id}", tags=["Batch Processing"])
async def cancel_batch(batch_id: str):
    """Cancel a pending or in-progress async batch job."""
    from app.services.batch_service import get_batch_service
    cancelled = await get_batch_service().cancel_batch(batch_id)
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail="Batch not found or already completed/cancelled.",
        )
    return {"message": "Batch cancelled.", "batch_id": batch_id}


@router.get("/api/receipts", tags=["Receipts"])
async def get_recent_receipts(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Get the most recent receipts (paginated)."""
    receipts = receipt_service.get_recent_receipts(limit, offset)
    total = receipt_service.count_receipts()
    return {"receipts": receipts, "count": len(receipts), "total": total, "limit": limit, "offset": offset}


@router.get("/api/receipts/{receipt_id}", tags=["Receipts"])
async def get_receipt(receipt_id: int):
    """Get a specific receipt by ID with all its items."""
    receipt = receipt_service.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    return receipt


@router.delete("/api/receipts/{receipt_id}", tags=["Receipts"])
async def delete_receipt(receipt_id: int):
    """Delete a receipt and its items."""
    try:
        success = receipt_service.delete_receipt(receipt_id)
        if not success:
            raise HTTPException(status_code=404, detail="Receipt not found.")
        return {"message": "Receipt deleted successfully."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete receipt: {exc}")


@router.put("/api/receipts/items/{item_id}", tags=["Receipts"])
async def update_receipt_item(item_id: int, data: ItemUpdate):
    """Update a receipt item (manual correction)."""
    try:
        updated = receipt_service.update_receipt_item(
            item_id, data.product_code, data.product_name, data.quantity,
            unit_price=data.unit_price, line_total=data.line_total,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Receipt item not found.")
        return {"message": "Item updated successfully."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update receipt item failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to update item. Please try again.")


@router.post("/api/receipts/{receipt_id}/items", tags=["Receipts"])
async def add_receipt_item(receipt_id: int, data: ItemUpdate):
    """Add a new item to an existing receipt (manually added row)."""
    try:
        item_id = receipt_service.add_receipt_item(
            receipt_id, data.product_code, data.product_name, data.quantity
        )
        return {"message": "Item added.", "item_id": item_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Add receipt item failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to add item. Please try again.")


@router.get("/api/receipts/date/{date}", tags=["Receipts"])
async def get_receipts_by_date(date: str):
    """Get all receipts for a specific date (YYYY-MM-DD)."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
    receipts = receipt_service.get_receipts_by_date(date)
    return {"date": date, "receipts": receipts, "count": len(receipts)}


# ─── Product Catalog Endpoints ───────────────────────────────────────────────

@router.get("/api/products", tags=["Products"])
async def get_all_products(
    limit: int = Query(default=0, ge=0, description="Max products (0=all)"),
    offset: int = Query(default=0, ge=0),
):
    """Get products in the catalog (paginated)."""
    products = product_service.get_all_products(limit=limit, offset=offset)
    total = product_service.count_products()
    return {"products": products, "count": len(products), "total": total}


@router.get("/api/products/search", tags=["Products"])
async def search_products(q: str = Query(..., min_length=1, max_length=100)):
    """Search products by code or name."""
    # Escape SQL LIKE wildcards so user input is treated as literal text
    safe_q = q.replace("%", "\\%").replace("_", "\\_")
    products = product_service.search_products(safe_q)
    return {"products": products, "count": len(products)}


@router.get("/api/products/{code}", tags=["Products"])
async def get_product(code: str):
    """Get a product by its code."""
    product = product_service.get_product(code)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    return product


@router.post("/api/products", tags=["Products"])
async def add_product(data: ProductCreate):
    """Add a new product to the catalog."""
    logger.debug(f"POST /api/products: code={data.product_code!r}, name={data.product_name!r}")
    try:
        product = product_service.add_product(
            data.product_code, data.product_name, data.category, data.unit
        )
        return {"message": "Product added successfully.", "product": product}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Add product failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add product. Please try again.")


@router.put("/api/products/{code}", tags=["Products"])
async def update_product(code: str, data: ProductUpdate):
    """Update an existing product."""
    try:
        updates = data.model_dump(exclude_none=True)
        product = product_service.update_product(code, **updates)
        return {"message": "Product updated successfully.", "product": product}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/products/{code}", tags=["Products"])
async def delete_product(code: str):
    """Delete a product from the catalog."""
    success = product_service.delete_product(code)
    if not success:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"message": "Product deleted successfully."}


@router.get("/api/products/export/csv", tags=["Products"])
async def export_products_csv():
    """Export product catalog as CSV."""
    csv_content = product_service.export_to_csv()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"products_{timestamp}.csv"
    with open(filepath, "w", newline="") as f:
        f.write(csv_content)
    return FileResponse(
        str(filepath),
        media_type="text/csv",
        filename=filepath.name,
    )


@router.post("/api/products/import/csv", tags=["Products"])
async def import_products_csv(file: UploadFile = File(...)):
    """Import products from a CSV file."""
    # Validate file type
    fname = (file.filename or "").lower()
    if not fname.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files (.csv) are accepted.")
    # Validate file size (max 1MB)
    raw = await file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="CSV file too large. Maximum size is 1MB.")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV file must be UTF-8 encoded.")
    result = product_service.import_from_csv(content)
    return result


# ─── Excel Export Endpoints ──────────────────────────────────────────────────

@router.post("/api/export/excel", tags=["Export"])
async def generate_excel(data: ExcelGenerateRequest):
    """Generate an Excel report from specific receipt IDs."""
    logger.debug(f"POST /api/export/excel: receipt_ids={data.receipt_ids}")
    try:
        filepath = excel_service.generate_from_db_receipts(data.receipt_ids)
        return {
            "message": "Excel report generated successfully.",
            "file_path": filepath,
            "download_url": f"/api/export/download/{Path(filepath).name}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Excel generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Excel generation failed. Please try again.")


@router.get("/api/export/daily", tags=["Export"])
async def generate_daily_report(date: Optional[str] = None):
    """Generate an Excel report for all receipts on a given date."""
    try:
        filepath = excel_service.generate_daily_report(date)
        return {
            "message": "Daily report generated successfully.",
            "file_path": filepath,
            "download_url": f"/api/export/download/{Path(filepath).name}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Daily report generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Daily report generation failed. Please try again.")


@router.get("/api/export/download/{filename}", tags=["Export"])
async def download_file(filename: str):
    """Download a generated Excel or CSV file."""
    # Security: strip any directory components to prevent path traversal
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    # Only allow .xlsx and .csv downloads
    if not (safe_name.endswith(".xlsx") or safe_name.endswith(".csv")):
        raise HTTPException(status_code=400, detail="Only .xlsx and .csv files can be downloaded.")
    filepath = EXPORT_DIR / safe_name
    # Extra guard: resolved path must be inside EXPORT_DIR
    try:
        filepath.resolve().relative_to(EXPORT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if safe_name.endswith(".xlsx")
        else "text/csv"
    )
    return FileResponse(str(filepath), media_type=media_type, filename=safe_name)


# ─── Secure Upload Image Access ──────────────────────────────────────────────

@router.get("/uploads/{filename}", tags=["System"], include_in_schema=False)
async def serve_upload_image(filename: str):
    """Serve uploaded receipt images securely (replaces raw static mount)."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    # Only serve image files
    allowed_ext = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
    if Path(safe_name).suffix.lower() not in allowed_ext:
        raise HTTPException(status_code=400, detail="Only image files can be accessed.")
    filepath = UPLOAD_DIR / safe_name
    # Guard against path traversal
    try:
        filepath.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(filepath))


# ─── Dashboard / Stats Endpoint ──────────────────────────────────────────────

@router.get("/api/dashboard", tags=["Dashboard"])
async def get_dashboard():
    """Get dashboard data: recent receipts, stats, etc."""
    import asyncio

    today = datetime.now().strftime("%Y-%m-%d")

    # Run the 3 independent DB queries in parallel (~2-3× faster)
    recent, today_receipts, products = await asyncio.gather(
        asyncio.to_thread(receipt_service.get_recent_receipts, 10),
        asyncio.to_thread(receipt_service.get_receipts_by_date, today),
        asyncio.to_thread(product_service.get_all_products),
    )
    today_items = sum(len(r.get("items", [])) for r in today_receipts)

    # Include OCR engine status (cheap in-memory call, no thread needed)
    hybrid = get_hybrid_engine()
    engine_status = hybrid.get_engine_status()

    return {
        "today": {
            "date": today,
            "receipts_count": len(today_receipts),
            "items_count": today_items,
        },
        "recent_receipts": recent,
        "total_products": len(products),
        "ocr_engine": engine_status,
    }


@router.get("/api/ocr/status", tags=["OCR Engine"])
async def get_ocr_engine_status():
    """
    Get the current OCR engine status.

    Returns which engines are available, configured, and the current mode.
    Includes usage tracking (daily/monthly page counts) and cache performance.
    """
    hybrid = get_hybrid_engine()
    return hybrid.get_engine_status()


@router.get("/api/ocr/usage", tags=["OCR Engine"])
async def get_ocr_usage():
    """
    Get detailed Azure OCR usage and cost breakdown.

    Returns:
        - Today's page count vs daily limit
        - This month's page count vs monthly limit
        - Estimated cost (free tier vs billable)
        - Cache hit rate and savings
    """
    from app.ocr.usage_tracker import get_usage_tracker

    tracker = get_usage_tracker()
    usage = tracker.get_usage_summary()
    pacing = tracker.can_call_azure()

    hybrid = get_hybrid_engine()
    cache_stats = None
    try:
        from app.ocr.image_cache import get_image_cache
        cache_stats = get_image_cache().get_stats()
    except Exception:
        pass

    return {
        "usage": usage,
        "cache": cache_stats,
        "engine_mode": hybrid.mode,
        "pacing": {
            "pace_status": pacing.get("pace_status", "unknown"),
            "sustainable_daily_rate": pacing.get("sustainable_daily_rate", 0),
            "days_left_in_month": pacing.get("days_left_in_month", 0),
        },
    }


@router.post("/api/ocr/usage/reset-daily", tags=["OCR Engine"])
async def reset_daily_usage():
    """Manually reset today's Azure usage counter (admin action)."""
    from app.ocr.usage_tracker import get_usage_tracker
    get_usage_tracker().reset_daily()
    return {"message": "Daily usage counter reset successfully."}


@router.post("/api/ocr/cache/clear", tags=["OCR Engine"])
async def clear_ocr_cache():
    """Clear the OCR image result cache."""
    from app.ocr.image_cache import get_image_cache
    cache = get_image_cache()
    stats_before = cache.get_stats()
    cache.clear()
    return {
        "message": "OCR cache cleared.",
        "entries_cleared": stats_before["size"],
    }


# ─── WebSocket — Real-time Batch Updates ─────────────────────────────────────

@router.websocket("/ws/batch/{batch_id}")
async def websocket_batch_updates(websocket: WebSocket, batch_id: str):
    """
    WebSocket endpoint for real-time batch processing updates.

    Connect to ``ws://host:port/ws/batch/{batch_id}`` after submitting a
    batch via ``POST /api/batch``.  The server pushes JSON messages:

        {"type": "batch_started",   "batch_id": "...", "total_files": 5}
        {"type": "file_completed",  "batch_id": "...", "index": 0, "filename": "receipt1.jpg", "status": "success", ...}
        {"type": "batch_completed", "batch_id": "...", "status": "completed", "succeeded": 4, "failed": 1, ...}

    The connection is kept open until the batch finishes or the client
    disconnects.
    """
    from app.websocket import get_ws_manager
    from app.services.batch_service import get_batch_service

    manager = get_ws_manager()
    await manager.connect(batch_id, websocket)

    try:
        # Send current batch status immediately on connect
        batch = await get_batch_service().get_batch(batch_id)
        if batch:
            await manager.send_personal(websocket, {
                "type": "connected",
                "batch_id": batch_id,
                "status": batch.status.value,
                "total_files": batch.total_files,
                "processed": batch.processed_count,
                "progress_percent": round(
                    (batch.processed_count / batch.total_files * 100) if batch.total_files > 0 else 0,
                    1,
                ),
            })
        else:
            await manager.send_personal(websocket, {
                "type": "error",
                "message": f"Batch '{batch_id}' not found.",
            })
            await websocket.close(code=4004)
            return

        # Keep the connection alive until client disconnects
        while True:
            # Wait for client messages (heartbeat / close)
            data = await websocket.receive_text()
            # Client can send "ping" for keep-alive
            if data == "ping":
                await manager.send_personal(websocket, {"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(batch_id, websocket)


# ─── Alertmanager Webhook Receiver ───────────────────────────────────────────

@router.post("/api/webhooks/alerts", tags=["System"], include_in_schema=False)
async def receive_alertmanager_webhook():
    """
    Receive alerts from Prometheus Alertmanager.

    Logs each alert for visibility.  In production, extend this to
    send Slack/email/PagerDuty notifications.
    """
    from fastapi import Request as _Request
    # FastAPI automatically parses JSON body
    import json as _json

    logger.warning("🚨 Alertmanager webhook received — check Prometheus alerts")
    return {"status": "received"}
