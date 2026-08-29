"""
security.py

Production security middleware: API key authentication, rate limiting,
and request validation.
"""

import logging
import time
from typing import Optional
from functools import wraps

from fastapi import Request, HTTPException, status, Depends
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from config import settings


logger = logging.getLogger(__name__)


# ============================================================================
# Rate Limiting
# ============================================================================

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.rate_limit_requests}/{settings.rate_limit_window}seconds"],
    enabled=settings.rate_limit_enabled,
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Custom handler for rate limit exceeded errors"""
    logger.warning(
        f"Rate limit exceeded: {request.client.host} | {request.method} {request.url.path}"
    )
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded. Please try again later."
    )


# ============================================================================
# API Key Authentication
# ============================================================================

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    """
    Verify API key if authentication is enabled.

    If settings.api_key is None, authentication is disabled.
    If settings.api_key is set, all requests must include matching X-API-Key header.
    """
    # Authentication disabled
    if not settings.api_key:
        return None

    # Authentication enabled but no key provided
    if not api_key:
        logger.warning("API request rejected: Missing API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Include 'X-API-Key' header.",
        )

    # Invalid API key
    if api_key != settings.api_key:
        logger.warning(f"API request rejected: Invalid API key (provided: {api_key[:8]}...)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return api_key


# ============================================================================
# Request Logging Middleware
# ============================================================================

async def log_requests_middleware(request: Request, call_next):
    """
    Log all API requests with timing and status codes.
    """
    start_time = time.time()

    # Skip logging for health checks (too noisy)
    skip_paths = ["/health", "/metrics"]
    if request.url.path in skip_paths:
        return await call_next(request)

    # Process request
    response = await call_next(request)

    # Calculate duration
    duration_ms = (time.time() - start_time) * 1000

    # Log the request
    from logging_config import log_api_request
    log_api_request(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
        user_agent=request.headers.get("user-agent"),
    )

    return response


# ============================================================================
# Input Validation
# ============================================================================

def validate_date_format(date_str: str) -> bool:
    """Validate YYYYMMDD date format"""
    if not date_str or len(date_str) != 8:
        return False
    try:
        int(date_str)
        return True
    except ValueError:
        return False


def sanitize_item_name(item_name: str) -> str:
    """Sanitize stock item name to prevent injection attacks"""
    # Remove any XML/SQL injection characters
    dangerous_chars = ["<", ">", "'", '"', ";", "--", "/*", "*/", "\\"]
    sanitized = item_name
    for char in dangerous_chars:
        sanitized = sanitized.replace(char, "")
    return sanitized.strip()


# ============================================================================
# Security Headers Middleware
# ============================================================================

async def add_security_headers_middleware(request: Request, call_next):
    """
    Add security headers to all responses.
    """
    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Don't cache sensitive API responses
    if request.url.path.startswith("/analytics") or request.url.path.startswith("/stock"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

    return response
