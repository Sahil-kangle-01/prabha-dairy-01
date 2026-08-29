"""
api/deps.py

Shared FastAPI dependencies.
"""

from __future__ import annotations

from typing import Generator

from sqlalchemy.orm import Session

from database.db import SessionLocal


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
