import asyncio
import json
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite

from server.config import settings


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobCanceled(Exception):
    """Задачу отменил пользователь (удалил) — обработку прерываем."""


# id задач, по которым запрошена отмена. Воркер проверяет это множество на
# границах этапов (через progress) и прерывает обработку, освобождая очередь.
_canceled_jobs: set[str] = set()


def request_cancel(job_id: str) -> None:
    _canceled_jobs.add(job_id)


def is_cancel_requested(job_id: str) -> bool:
    return job_id in _canceled_jobs


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    backend TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    output_path TEXT,
    language TEXT NOT NULL,
    num_speakers INTEGER,
    initial_prompt TEXT,
    progress REAL DEFAULT 0,
    stage TEXT,
    error TEXT,
    speaker_names_json TEXT,
    segments_json TEXT,
    mode TEXT NOT NULL DEFAULT 'speakers',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
"""
# Индексы по новым (мигрируемым) колонкам создаём ОТДЕЛЬНО, после ALTER —
# иначе на старой БД executescript упадёт «no such column: mode».


async def _ensure_column(db, table: str, column: str, sql_def: str) -> None:
    """SQLite не умеет ALTER TABLE IF NOT EXISTS — делаем сами."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    existing = {r[1] for r in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_def}")


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


async def init_db() -> None:
    settings.queue_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with _db() as db:
        await db.executescript(SCHEMA)
        # Миграции для существующих БД (создаем недостающие столбцы)
        await _ensure_column(db, "jobs", "mode", "TEXT NOT NULL DEFAULT 'speakers'")
        # Индексы по мигрируемым колонкам — после ALTER
        await db.execute(
            "CREATE INDEX IF NOT EXISTS jobs_mode_created ON jobs(mode, created_at)"
        )
        # Миграция значений mode на новые имена:
        await db.execute("UPDATE jobs SET mode = 'classic' WHERE mode IN ('speakers', 'pyannote')")
        await db.execute("UPDATE jobs SET mode = 'nemo-sortformer' WHERE mode = 'nemo'")
        # Восстановление после сбоя: задачи, прерванные на полпути (сервер
        # закрыли/упал во время обработки), иначе навсегда зависают в 'running' —
        # claim_next_pending их не подхватывает. Сбрасываем в 'pending', чтобы
        # воркер до-обработал их при следующем запуске.
        await db.execute(
            "UPDATE jobs SET status = 'pending', stage = NULL, progress = 0 "
            "WHERE status = 'running'"
        )
        await db.commit()


async def create_job(
    *,
    backend: str,
    audio_path: Path,
    language: str,
    num_speakers: int | None,
    initial_prompt: str | None,
    mode: str = "speakers",
) -> str:
    job_id = uuid.uuid4().hex
    now = _now_iso()
    async with _db() as db:
        await db.execute(
            """
            INSERT INTO jobs (id, status, backend, audio_path, language,
                              num_speakers, initial_prompt, mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                JobStatus.PENDING.value,
                backend,
                str(audio_path),
                language,
                num_speakers,
                initial_prompt,
                mode,
                now,
                now,
            ),
        )
        await db.commit()
    return job_id


async def get_job(job_id: str) -> dict[str, Any] | None:
    async with _db() as db:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_jobs(limit: int = 100, mode: str | None = None) -> list[dict[str, Any]]:
    async with _db() as db:
        if mode is None:
            query = (
                "SELECT id, status, backend, progress, stage, error, "
                "audio_path, output_path, language, num_speakers, mode, "
                "created_at, updated_at "
                "FROM jobs ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple = (limit,)
        else:
            query = (
                "SELECT id, status, backend, progress, stage, error, "
                "audio_path, output_path, language, num_speakers, mode, "
                "created_at, updated_at "
                "FROM jobs WHERE mode = ? ORDER BY created_at DESC LIMIT ?"
            )
            params = (mode, limit)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_job(job_id: str) -> None:
    async with _db() as db:
        await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()


async def claim_next_pending() -> dict[str, Any] | None:
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at LIMIT 1",
            (JobStatus.PENDING.value,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.commit()
            return None
        await db.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (JobStatus.RUNNING.value, _now_iso(), row["id"]),
        )
        await db.commit()
        return dict(row)


async def update_progress(job_id: str, progress: float, stage: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE jobs SET progress = ?, stage = ?, updated_at = ? WHERE id = ?",
            (progress, stage, _now_iso(), job_id),
        )
        await db.commit()


async def complete_job(
    job_id: str,
    *,
    segments: list[dict],
    output_path: Path,
) -> None:
    async with _db() as db:
        await db.execute(
            """
            UPDATE jobs
            SET status = ?, progress = 1.0, stage = 'done',
                segments_json = ?, output_path = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                JobStatus.DONE.value,
                json.dumps(segments, ensure_ascii=False),
                str(output_path),
                _now_iso(),
                job_id,
            ),
        )
        await db.commit()


async def fail_job(job_id: str, error: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (JobStatus.FAILED.value, error, _now_iso(), job_id),
        )
        await db.commit()


async def update_speaker_names(job_id: str, mapping: dict[str, str]) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE jobs SET speaker_names_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(mapping, ensure_ascii=False), _now_iso(), job_id),
        )
        await db.commit()


async def run_worker_loop(handler) -> None:
    while True:
        job = await claim_next_pending()
        if job is None:
            await asyncio.sleep(2.0)
            continue
        try:
            await handler(job)
        except JobCanceled:
            # Отменено пользователем — задача уже удалена из БД, просто идём
            # дальше и освобождаем очередь для следующих.
            pass
        except Exception as exc:
            # Полный стек в консоль run.bat — короткого сообщения в UI мало,
            # чтобы понять, на каком этапе/в каком модуле упало.
            traceback.print_exc()
            await fail_job(job["id"], f"{type(exc).__name__}: {exc}")
        finally:
            _canceled_jobs.discard(job["id"])
