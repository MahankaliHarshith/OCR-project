# ─── Build Stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies for OpenCV and image processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Runtime Stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="Mahankali Harshith"
LABEL description="Handwritten Receipt Scanner — OCR-powered receipt digitization"

# Install runtime system dependencies + tini (proper PID 1 init process)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/
COPY run.py .
COPY requirements.txt .
COPY .env.example .

# Create runtime directories with correct ownership
RUN mkdir -p uploads exports models logs data backups \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# EasyOCR model cache — persisted via volume mount
ENV EASYOCR_MODULE_PATH=/app/models
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

# tini ensures proper signal handling (SIGTERM) and zombie process reaping.
# Without it, Python as PID 1 won't propagate signals for graceful shutdown.
ENTRYPOINT ["tini", "--"]

# Production server: single worker (OCR models are ~1.6GB each, multi-worker
# would multiply memory). Use external scaling (multiple containers) instead.
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-keep-alive", "30", \
     "--timeout-graceful-shutdown", "15", \
     "--access-log"]
