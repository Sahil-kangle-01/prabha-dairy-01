"""
monitoring.py

Health checks, metrics, and monitoring endpoints for production observability.
"""

import logging
import time
import psutil
from datetime import datetime
from typing import Dict, Any

from fastapi import APIRouter, Response
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

from database.db import check_database_health, engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["monitoring"])

# Prometheus metrics
request_count = Counter(
    "prabha_dairy_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"]
)

request_duration = Histogram(
    "prabha_dairy_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint"]
)

sync_operations = Counter(
    "prabha_dairy_sync_operations_total",
    "Total sync operations",
    ["sync_type", "status"]
)

db_connections = Gauge(
    "prabha_dairy_db_connections",
    "Database connection pool status",
    ["state"]
)


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """
    Basic health check endpoint.
    Returns 200 if service is running, 503 if unhealthy.

    Used by load balancers and monitoring systems.
    """
    db_health = check_database_health()

    health_status = {
        "status": "healthy" if db_health["status"] == "healthy" else "unhealthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "prabha-dairy-api",
        "database": db_health,
    }

    if health_status["status"] == "unhealthy":
        return Response(
            content=str(health_status),
            status_code=503,
            media_type="application/json"
        )

    return health_status


@router.get("/health/live")
async def liveness_probe():
    """
    Kubernetes liveness probe - checks if process is alive.
    """
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


@router.get("/health/ready")
async def readiness_probe():
    """
    Kubernetes readiness probe - checks if service can accept traffic.
    """
    db_health = check_database_health()

    if db_health["status"] != "healthy":
        return Response(
            content='{"status": "not_ready", "reason": "database_unavailable"}',
            status_code=503,
            media_type="application/json"
        )

    return {"status": "ready", "timestamp": datetime.utcnow().isoformat()}


@router.get("/metrics")
async def metrics():
    """
    Prometheus metrics endpoint.
    Scrape this from your monitoring system.
    """
    # Update database connection metrics
    try:
        pool = engine.pool
        db_connections.labels(state="checked_in").set(pool.checkedin())
        db_connections.labels(state="checked_out").set(pool.checkedout())
        db_connections.labels(state="overflow").set(pool.overflow())
    except Exception as e:
        logger.error(f"Failed to update DB metrics: {e}")

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/status")
async def system_status() -> Dict[str, Any]:
    """
    Detailed system status for debugging and monitoring.
    Includes process info, memory, database stats.
    """
    process = psutil.Process()

    # Memory info
    memory_info = process.memory_info()
    memory_mb = memory_info.rss / 1024 / 1024

    # CPU info
    cpu_percent = process.cpu_percent(interval=0.1)

    # Database pool info
    db_health = check_database_health()

    # Uptime
    create_time = datetime.fromtimestamp(process.create_time())
    uptime_seconds = (datetime.now() - create_time).total_seconds()

    return {
        "service": "prabha-dairy-api",
        "timestamp": datetime.utcnow().isoformat(),
        "uptime_seconds": uptime_seconds,
        "process": {
            "pid": process.pid,
            "memory_mb": round(memory_mb, 2),
            "cpu_percent": cpu_percent,
            "num_threads": process.num_threads(),
        },
        "database": db_health,
        "system": {
            "cpu_count": psutil.cpu_count(),
            "total_memory_mb": round(psutil.virtual_memory().total / 1024 / 1024, 2),
            "available_memory_mb": round(psutil.virtual_memory().available / 1024 / 1024, 2),
        }
    }
