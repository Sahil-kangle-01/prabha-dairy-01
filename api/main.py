"""
api/main.py

Production-ready FastAPI backend for Prabha Dairy.

Run in production:
    gunicorn api.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

Run in development:
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded

# Initialize logging first
from logging_config import setup_logging
setup_logging("prabha_dairy_api")

logger = logging.getLogger(__name__)

# Import configuration and validate
from config import settings, validate_environment
from security import (
    limiter,
    rate_limit_exceeded_handler,
    log_requests_middleware,
    add_security_headers_middleware,
)
from monitoring import router as monitoring_router
from api.routes import analytics, stock, sync as sync_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle management: startup and shutdown handlers.
    """
    # Startup
    logger.info("=" * 70)
    logger.info("Starting Prabha Dairy API Server")
    logger.info("=" * 70)

    validate_environment()

    # Verify database connectivity
    from database.db import check_database_health
    db_health = check_database_health()
    if db_health["status"] != "healthy":
        logger.error(f"Database health check failed: {db_health}")
        logger.error("Cannot start server without database connection")
        sys.exit(1)

    logger.info("[OK] Database connection verified")
    logger.info("[OK] Server startup complete")
    logger.info("=" * 70)

    yield

    # Shutdown
    logger.info("Shutting down Prabha Dairy API Server...")
    logger.info("[OK] Graceful shutdown complete")


app = FastAPI(
    title="Prabha Dairy API",
    description="Production backend for Prabha Dairy: analytics, live stock lookup, and sync management.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,  # Disable Swagger in production
    redoc_url="/redoc" if not settings.is_production else None,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# CORS - production-safe configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    max_age=3600,  # Cache preflight requests for 1 hour
)

# Security and logging middleware
app.middleware("http")(add_security_headers_middleware)
app.middleware("http")(log_requests_middleware)

# Global exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors gracefully"""
    logger.warning(f"Validation error on {request.url.path}: {exc.errors()}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Invalid request data",
            "errors": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler to prevent leaking stack traces"""
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)

    # Don't leak internal errors in production
    if settings.is_production:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )
    else:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )


# Include routers
app.include_router(monitoring_router)
app.include_router(analytics.router)
app.include_router(stock.router)
# app.include_router(print_routes.router)
app.include_router(sync_routes.router)

# Static files & dashboard
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
def dashboard():
    """Redirect root to the unified app."""
    return RedirectResponse(url="/app")


@app.get("/app")
def unified_app():
    """Serve the unified SPA dashboard."""
    return FileResponse(str(_STATIC_DIR / "index.html"))


@app.get("/dashboard")
def dashboard_legacy():
    """Serve the legacy standalone dashboard."""
    return FileResponse(str(_STATIC_DIR / "dashboard.html"))


@app.get("/stock-lookup")
def stock_lookup_page():
    """Serve the live stock lookup page."""
    return FileResponse(str(_STATIC_DIR / "stock-lookup.html"))


# Graceful shutdown handling
def handle_sigterm(signum, frame):
    """Handle SIGTERM for graceful shutdown"""
    logger.info("Received SIGTERM, shutting down gracefully...")
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_sigterm)
