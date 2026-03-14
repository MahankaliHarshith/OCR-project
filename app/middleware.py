"""
Security Middleware for the Receipt Scanner API.

Provides:
  1. Rate limiting (per-IP, sliding window)
  2. API key protection for destructive endpoints
  3. Dynamic CORS for Dev Tunnels URLs
"""

import time
import logging
import threading
from collections import defaultdict
from typing import Dict, List, Tuple

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

try:
    from app.metrics import record_rate_limit as _record_rate_limit
except Exception:
    def _record_rate_limit(endpoint_type: str = "general") -> None:
        pass

logger = logging.getLogger(__name__)

# Trusted reverse-proxy IPs that are allowed to set X-Forwarded-For.
# Only these IPs' X-Forwarded-For headers are trusted; all others use
# the direct connection IP.  Prevents rate-limit bypass via header spoofing.
# Configure via TRUSTED_PROXIES env var (comma-separated) or defaults below.
import os as _os
_trusted_env = _os.getenv("TRUSTED_PROXIES", "")
TRUSTED_PROXY_IPS: set[str] = (
    {ip.strip() for ip in _trusted_env.split(",") if ip.strip()}
    if _trusted_env
    else {"127.0.0.1", "::1", "172.17.0.1"}  # localhost + default Docker gateway
)


# ─── Security Headers ─────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds HTTP security headers to every response.
    Hardens against clickjacking, MIME sniffing, info leakage.
    """
    _HEADERS = {
        # X-Frame-Options removed: VS Code Simple Browser uses a vscode-webview://
        # origin, so even SAMEORIGIN blocks rendering.  The CORS middleware already
        # restricts cross-origin access; clickjacking risk is minimal for a local tool.
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-XSS-Protection": "0",          # Disable legacy XSS filter (can cause issues; CSP is better)
        "Permissions-Policy": "camera=self, microphone=()",
        # COOP removed: can interfere with Simple Browser window management
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss: https://*.devtunnels.ms https://*.github.dev"
        ),
    }

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in self._HEADERS.items():
            response.headers.setdefault(header, value)
        return response


# ─── Rate Limiter ────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Simple in-memory sliding window rate limiter.
    Tracks requests per client IP within a rolling 60-second window.
    Thread-safe via threading.Lock.
    """

    def __init__(self):
        # {ip: [(timestamp, ...), ...]}
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._cleanup_counter = 0
        self._lock = threading.Lock()

    def is_allowed(self, client_ip: str, limit: int, window_seconds: int = 60) -> Tuple[bool, int]:
        """
        Check if a request from client_ip is within rate limits.

        Returns:
            (allowed: bool, remaining: int)
        """
        with self._lock:
            now = time.time()
            cutoff = now - window_seconds

            # Clean old entries for this IP
            timestamps = self._requests[client_ip]
            self._requests[client_ip] = [t for t in timestamps if t > cutoff]

            current_count = len(self._requests[client_ip])

            if current_count >= limit:
                return False, 0

            self._requests[client_ip].append(now)

            # Periodic cleanup of stale IPs (every 50 calls)
            self._cleanup_counter += 1
            if self._cleanup_counter >= 50:
                self._cleanup_counter = 0
                self._cleanup(cutoff)

            return True, limit - current_count - 1

    def _cleanup(self, cutoff: float):
        """Remove IPs with no recent requests."""
        stale = [ip for ip, ts in self._requests.items() if not ts or max(ts) < cutoff]
        for ip in stale:
            del self._requests[ip]


# Singleton rate limiter
_rate_limiter = RateLimiter()


# ─── Rate Limit Middleware ───────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiting middleware.
    Applies stricter limits to expensive endpoints (scan).
    """

    def __init__(self, app, general_rpm: int = 30, scan_rpm: int = 10):
        super().__init__(app)
        self.general_rpm = general_rpm
        self.scan_rpm = scan_rpm

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for static files and docs
        path = request.url.path
        if path.startswith("/static") or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)

        # Determine client IP for rate-limiting.
        # SECURITY: Only trust X-Forwarded-For from known reverse proxies.
        # If the direct connection IP is not in TRUSTED_PROXY_IPS, ignore
        # X-Forwarded-For entirely — otherwise any client can spoof their IP
        # and bypass rate limits.
        direct_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded and direct_ip in TRUSTED_PROXY_IPS:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = direct_ip

        # Determine limit based on endpoint — scan + batch share a stricter budget
        is_scan_endpoint = (
            path == "/api/receipts/scan"
            or path == "/api/receipts/scan-batch"
            or path == "/api/batch"
        )
        limit = self.scan_rpm if is_scan_endpoint else self.general_rpm

        # Use composite key so scan and general budgets don't mix.
        # Without this, general API calls consume scan budget and vice versa.
        rate_key = f"{client_ip}:scan" if is_scan_endpoint else f"{client_ip}:general"
        allowed, remaining = _rate_limiter.is_allowed(rate_key, limit)

        if not allowed:
            logger.warning(f"Rate limit exceeded: {client_ip} on {path}")
            _record_rate_limit("scan" if is_scan_endpoint else "general")
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a moment and try again."},
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


# ─── API Key Guard ───────────────────────────────────────────────────────────

# Protected destructive endpoints
PROTECTED_PATHS = {
    ("DELETE", "/api/receipts/"),
    ("DELETE", "/api/products/"),
    ("POST", "/api/ocr/usage/reset-daily"),
    ("POST", "/api/ocr/cache/clear"),
}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Optional API key check for destructive operations.
    Only active if API_SECRET_KEY is configured in config/env.
    """

    def __init__(self, app, api_key: str = ""):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if not self.api_key:
            # No key configured — skip protection (dev mode)
            return await call_next(request)

        method = request.method
        path = request.url.path

        # Check if this is a protected endpoint
        is_protected = False
        for pmethod, ppath in PROTECTED_PATHS:
            if method == pmethod and path.startswith(ppath):
                is_protected = True
                break

        if is_protected:
            # Allow same-origin requests from the browser frontend without API key.
            # Browsers set Sec-Fetch-Site automatically; external tools (curl, scripts) don't.
            fetch_site = request.headers.get("Sec-Fetch-Site", "")
            referer = request.headers.get("Referer", "")
            origin = request.headers.get("Origin", "")
            host = request.headers.get("Host", "")
            is_same_origin = (
                fetch_site == "same-origin"
                or (referer and host and host in referer)
                or (origin and host and host in origin)
            )
            if not is_same_origin:
                provided_key = request.headers.get("X-API-Key", "")
                if provided_key != self.api_key:
                    logger.warning(f"Unauthorized {method} {path} from {request.client.host if request.client else '?'}")
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Invalid or missing API key. Set X-API-Key header."},
                    )

        return await call_next(request)


# ─── Dynamic CORS for Dev Tunnels ────────────────────────────────────────────

class DevTunnelCORSMiddleware(BaseHTTPMiddleware):
    """
    Dynamically allows CORS for VS Code Dev Tunnels URLs.
    Dev Tunnel URLs match: https://*.devtunnels.ms
    This avoids hard-coding tunnel IDs which change each session.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # If origin is a Dev Tunnel, add it to allowed origins dynamically
        # Use proper URL parsing to prevent subdomain-spoofing bypasses
        if origin:
            from urllib.parse import urlparse
            try:
                hostname = urlparse(origin).hostname or ""
            except Exception:
                hostname = ""
            is_tunnel = (
                hostname.endswith(".devtunnels.ms")
                or hostname.endswith(".github.dev")
            )
            if is_tunnel:
                cors_headers = {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
                    "Access-Control-Max-Age": "600",
                }
                # Immediately resolve preflight without hitting the router
                if request.method == "OPTIONS":
                    return Response(status_code=200, headers=cors_headers)

                response = await call_next(request)
                for k, v in cors_headers.items():
                    response.headers[k] = v
                return response

        return await call_next(request)
