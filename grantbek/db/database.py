"""Async SQLite access layer built on aiosqlite.

A single shared connection is used for the app lifetime. Rows come back as
``aiosqlite.Row`` so handlers can use dict-style access (``row["title"]``).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

_conn: Optional[aiosqlite.Connection] = None


async def get_connection() -> aiosqlite.Connection:
    """Return the shared connection, opening it on first use."""
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(settings.database_path)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL;")
        await _conn.execute("PRAGMA foreign_keys=ON;")
        await _conn.commit()
        logger.info("Opened SQLite database at %s", settings.database_path)
    return _conn


async def close_connection() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None
        logger.info("Closed SQLite database")


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    conn = await get_connection()
    await conn.execute(sql, tuple(params))
    await conn.commit()


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    conn = await get_connection()
    async with conn.execute(sql, tuple(params)) as cur:
        return list(await cur.fetchall())


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
    conn = await get_connection()
    async with conn.execute(sql, tuple(params)) as cur:
        return await cur.fetchone()
