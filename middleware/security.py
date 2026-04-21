"""
Security middleware for LendFlow API (fintech-grade).
─────────────────────────────────────────────────────
• Rate limiting (in-memory token bucket per IP)
• Security headers (HSTS, X-Content-Type, X-Frame-Options, CSP, etc.)
• Request ID tracking
• IP extraction from X-Forwarded-For (proxy-aware)
"""

import time
import uuid
from collections import defaultdict
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


# ═══════════════════════════════════════════════
#  RATE LIMITER (token bucket per IP)
# ═══════════════════════════════════════════════

class _TokenBucket:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate          # tokens per second
        self.capacity = capacity
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_time)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            tokens, last = self._buckets.get(key, (self.capacity, now))
            elapsed = now - last
            tokens = min(self.capacity, tokens + elapsed * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True
            self._buckets[key] = (tokens, now)
            return False

    def cleanup(self, max_age: float = 3600):
        """Remove stale entries to prevent memory leaks."""
        with self._lock:
            now = time.monotonic()
            stale = [k for k, (_, t) in self._buckets.items() if now - t > max_age]
            for k in stale:
                del self._buckets[k]


# Global rate limiter: 60 requests/minute per IP for general endpoints
_general_limiter = _TokenBucket(rate=1.0, capacity=60)

# Strict limiter for auth endpoints: 10 requests/minute per IP
_auth_limiter = _TokenBucket(rate=10 / 60, capacity=10)

# Aggressive paths that need stricter limits
_AUTH_PATHS = {
    "/auth/login", "/auth/register", "/auth/send_otp", "/auth/verify_otp",
    "/auth/send_phone_otp", "/auth/verify_phone_otp",
    "/auth/send_password_reset_code", "/auth/reset_password",
    "/auth/refresh",
}


def _get_client_ip(request: Request) -> str:
    """Extract real IP, respecting X-Forwarded-For behind a reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # First IP in the chain is the real client
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ═══════════════════════════════════════════════
#  SECURITY HEADERS MIDDLEWARE
# ═══════════════════════════════════════════════

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds OWASP-recommended security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Attach request ID for tracing
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        path = request.url.path

        response = await call_next(request)

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Docs UIs need inline scripts/styles and CDN assets. Keep strict CSP for all other routes.
        if path.startswith("/docs") or path.startswith("/redoc") or path == "/openapi.json":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https:; "
                "font-src 'self' data: https:"
            )
        else:
            response.headers["Content-Security-Policy"] = "default-src 'self'"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Request-ID"] = request_id

        # Don't expose server info
        if "server" in response.headers:
            del response.headers["server"]

        return response


# ═══════════════════════════════════════════════
#  RATE LIMITING MIDDLEWARE
# ═══════════════════════════════════════════════

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting — strict for auth endpoints, lenient for others."""

    _cleanup_counter = 0

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        client_ip = _get_client_ip(request)
        path = request.url.path.rstrip("/")

        # Pick the right limiter
        if path in _AUTH_PATHS:
            limiter = _auth_limiter
            limit_name = "10/min"
        else:
            limiter = _general_limiter
            limit_name = "60/min"

        if not limiter.allow(client_ip):
            return Response(
                content='{"detail":"Rate limit exceeded. Please slow down."}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        # Periodic cleanup (every 1000 requests)
        RateLimitMiddleware._cleanup_counter += 1
        if RateLimitMiddleware._cleanup_counter % 1000 == 0:
            _general_limiter.cleanup()
            _auth_limiter.cleanup()

        response = await call_next(request)
        return response
