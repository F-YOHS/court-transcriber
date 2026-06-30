"""
БД и операции для профилей голосов (Speaker Enrollment).

Профиль = {id, name, embedding (256 float), sample_path, created_at}.
Хранится в SQLite той же БД что и очередь задач.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np

from server.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    sample_path TEXT,
    created_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def _db():
    db = await aiosqlite.connect(settings.queue_db_path)
    try:
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        await db.close()


async def init_profiles_table() -> None:
    settings.queue_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with _db() as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def create_profile(
    name: str,
    embedding: np.ndarray,
    sample_path: Path | None,
) -> str:
    profile_id = uuid.uuid4().hex
    emb_json = json.dumps(embedding.astype(float).tolist())
    async with _db() as db:
        await db.execute(
            "INSERT INTO voice_profiles (id, name, embedding_json, sample_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                profile_id,
                name,
                emb_json,
                str(sample_path) if sample_path else None,
                _now_iso(),
            ),
        )
        await db.commit()
    return profile_id


async def list_profiles() -> list[dict[str, Any]]:
    async with _db() as db:
        async with db.execute(
            "SELECT id, name, sample_path, created_at FROM voice_profiles ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_profile(profile_id: str) -> dict[str, Any] | None:
    async with _db() as db:
        async with db.execute(
            "SELECT * FROM voice_profiles WHERE id = ?", (profile_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_profile(profile_id: str) -> dict[str, Any] | None:
    profile = await get_profile(profile_id)
    if profile is None:
        return None
    async with _db() as db:
        await db.execute("DELETE FROM voice_profiles WHERE id = ?", (profile_id,))
        await db.commit()
    return profile


async def all_embeddings() -> list[tuple[str, str, np.ndarray]]:
    """[(profile_id, name, embedding_array), ...] — для матчинга."""
    async with _db() as db:
        async with db.execute(
            "SELECT id, name, embedding_json FROM voice_profiles"
        ) as cur:
            rows = await cur.fetchall()
    out: list[tuple[str, str, np.ndarray]] = []
    for r in rows:
        try:
            vec = np.asarray(json.loads(r["embedding_json"]), dtype=np.float32)
            out.append((r["id"], r["name"], vec))
        except Exception:
            continue
    return out
