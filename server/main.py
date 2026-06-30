import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.auth import (
    auth_enabled,
    auth_middleware,
    clear_session_cookie,
    issue_token,
    set_session_cookie,
    verify_credentials,
)
from server.backends.base import TranscriptSegment
from server.config import settings
from server.embedding import EmbeddingExtractor
from server.export import export_to_docx
from server.queue import (
    JobStatus,
    create_job,
    delete_job,
    get_job,
    init_db,
    list_jobs,
    request_cancel,
    run_worker_loop,
    update_speaker_names,
)
from server.voice_profiles import (
    create_profile,
    delete_profile,
    init_profiles_table,
    list_profiles,
)
from server.worker import process_job


class LoginRequest(BaseModel):
    username: str
    password: str


CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
ALLOWED_AUDIO_EXT = {
    ".mp3", ".wav", ".m4a", ".mp4", ".ogg", ".oga", ".opus",
    ".flac", ".webm", ".wma", ".aac", ".amr", ".3gp",
}

# Папка для незавершённых (докачиваемых) загрузок: <storage>/uploads/incoming.
INCOMING_DIR = settings.storage_dir / "uploads" / "incoming"
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Блокировки на upload_id, чтобы два чанка одной сессии не записались внахлёст.
_upload_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_profiles_table()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "uploads").mkdir(exist_ok=True)
    (settings.storage_dir / "outputs").mkdir(exist_ok=True)
    (settings.storage_dir / "profiles").mkdir(exist_ok=True)
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    # Подчищаем брошенные незавершённые докачки (старше 24ч), чтобы не копить мусор.
    cutoff = time.time() - 24 * 3600
    for leftover in INCOMING_DIR.glob("*"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError:
            pass

    worker_task = asyncio.create_task(run_worker_loop(process_job))
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Court Transcriber", version="0.0.1", lifespan=lifespan)
app.middleware("http")(auth_middleware)


_NO_CACHE_PREFIXES = ("/static/",)
_NO_CACHE_EXACT = {
    "/", "/login", "/profiles", "/text",
    "/classic", "/nemo-sortformer", "/nemo-msdd",
    # legacy:
    "/speakers", "/pyannote", "/nemo",
}


@app.middleware("http")
async def no_cache_for_ui(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in _NO_CACHE_EXACT or any(path.startswith(p) for p in _NO_CACHE_PREFIXES):
        response.headers["Cache-Control"] = "no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "backend": settings.asr_backend,
        "auth_enabled": auth_enabled(),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return (CLIENT_DIR / "login.html").read_text(encoding="utf-8")


@app.post("/api/login")
async def login(body: LoginRequest, response: Response) -> dict[str, bool]:
    if not auth_enabled():
        raise HTTPException(400, "Auth не настроен на сервере")
    if not verify_credentials(body.username, body.password):
        raise HTTPException(401, "Неверный логин или пароль")
    token = issue_token(body.username)
    set_session_cookie(response, token)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, bool]:
    clear_session_cookie(response)
    return {"ok": True}


_ALLOWED_MODES = {
    "text_only",
    "nemo-sortformer",
    "nemo-msdd",
    "classic",
    # legacy aliases:
    "nemo",      # -> nemo-sortformer
    "pyannote",  # -> classic
    "speakers",  # -> classic
}


@app.post("/api/jobs")
async def create_transcription_job(
    file: UploadFile = File(...),
    language: str = Form("ru"),
    num_speakers: int | None = Form(None),
    initial_prompt: str | None = Form(None),
    mode: str = Form("classic"),
) -> dict[str, str]:
    if mode not in _ALLOWED_MODES:
        raise HTTPException(400, f"mode должен быть одним из {sorted(_ALLOWED_MODES)}")
    # Legacy → новые имена
    if mode in ("speakers", "pyannote"):
        mode = "classic"
    elif mode == "nemo":
        mode = "nemo-sortformer"

    raw_name = Path(file.filename or "audio").name
    suffix = Path(raw_name).suffix.lower()
    if suffix and suffix not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Формат {suffix} не поддерживается. "
                   f"Поддерживаемые: {', '.join(sorted(ALLOWED_AUDIO_EXT))}",
        )

    uploads_dir = settings.storage_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    file_id = uuid.uuid4().hex
    audio_path = uploads_dir / f"{file_id}_{raw_name}"

    written = 0
    with audio_path.open("wb") as out:
        while True:
            chunk = await file.read(8 * 1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > settings.max_upload_bytes:
                out.close()
                audio_path.unlink(missing_ok=True)
                raise HTTPException(413, "Файл слишком большой")
            out.write(chunk)

    if written == 0:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(400, "Пустой файл")

    job_id = await create_job(
        backend=settings.asr_backend,
        audio_path=audio_path,
        language=language,
        num_speakers=num_speakers,
        initial_prompt=initial_prompt,
        mode=mode,
    )
    return {"job_id": job_id, "status": "pending"}


# ---------- Resumable (chunked) upload ----------
# Большие записи (заседания на 2-3 часа) грузятся кусками с возобновлением:
# при обрыве канала клиент дочитывает смещение с сервера и продолжает, а не
# начинает заново. Поток: init -> PUT чанки -> complete (создаёт job).

class UploadInit(BaseModel):
    filename: str
    size: int


class UploadComplete(BaseModel):
    language: str = "ru"
    num_speakers: int | None = None
    initial_prompt: str | None = None
    mode: str = "classic"


def _incoming_paths(upload_id: str) -> tuple[Path, Path]:
    return INCOMING_DIR / f"{upload_id}.part", INCOMING_DIR / f"{upload_id}.json"


def _validate_upload_id(upload_id: str) -> None:
    # Только hex-uuid — чтобы upload_id не мог стать обходом пути (path traversal).
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(400, "Некорректный upload_id")


@app.post("/api/uploads/init")
async def upload_init(body: UploadInit) -> dict:
    raw_name = Path(body.filename or "audio").name
    suffix = Path(raw_name).suffix.lower()
    if suffix and suffix not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            400,
            f"Формат {suffix} не поддерживается. "
            f"Поддерживаемые: {', '.join(sorted(ALLOWED_AUDIO_EXT))}",
        )
    if body.size <= 0:
        raise HTTPException(400, "Размер файла должен быть больше нуля")
    if body.size > settings.max_upload_bytes:
        raise HTTPException(413, "Файл слишком большой")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    part, meta = _incoming_paths(upload_id)
    part.touch()
    meta.write_text(
        json.dumps({"filename": raw_name, "size": body.size}),
        encoding="utf-8",
    )
    return {"upload_id": upload_id, "received": 0}


@app.get("/api/uploads/{upload_id}")
async def upload_status(upload_id: str) -> dict:
    _validate_upload_id(upload_id)
    part, meta = _incoming_paths(upload_id)
    if not part.exists() or not meta.exists():
        raise HTTPException(404, "Сессия загрузки не найдена")
    info = json.loads(meta.read_text(encoding="utf-8"))
    return {"received": part.stat().st_size, "size": info.get("size")}


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0):
    _validate_upload_id(upload_id)
    part, meta = _incoming_paths(upload_id)
    if not part.exists() or not meta.exists():
        raise HTTPException(404, "Сессия загрузки не найдена")

    lock = _upload_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        current = part.stat().st_size
        if offset != current:
            # Клиент рассинхронизировался — сообщаем реальное смещение (он до-синхронит).
            return JSONResponse({"received": current}, status_code=409)
        body = await request.body()
        if current + len(body) > settings.max_upload_bytes:
            raise HTTPException(413, "Файл слишком большой")
        with part.open("ab") as fh:
            fh.write(body)
        return JSONResponse({"received": current + len(body)})


@app.post("/api/uploads/{upload_id}/complete")
async def upload_complete(upload_id: str, body: UploadComplete) -> dict[str, str]:
    _validate_upload_id(upload_id)
    part, meta = _incoming_paths(upload_id)
    if not part.exists() or not meta.exists():
        raise HTTPException(404, "Сессия загрузки не найдена")

    info = json.loads(meta.read_text(encoding="utf-8"))
    declared = int(info.get("size", -1))
    actual = part.stat().st_size
    if declared >= 0 and actual != declared:
        raise HTTPException(
            400,
            f"Загрузка неполная: получено {actual} из {declared} байт. Повтори загрузку.",
        )

    mode = body.mode
    if mode not in _ALLOWED_MODES:
        raise HTTPException(400, f"mode должен быть одним из {sorted(_ALLOWED_MODES)}")
    if mode in ("speakers", "pyannote"):
        mode = "classic"
    elif mode == "nemo":
        mode = "nemo-sortformer"

    raw_name = Path(info.get("filename") or "audio").name
    uploads_dir = settings.storage_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    audio_path = uploads_dir / f"{file_id}_{raw_name}"
    # part и audio_path в одной папке uploads/ — перенос атомарный (os.replace).
    part.replace(audio_path)
    meta.unlink(missing_ok=True)
    _upload_locks.pop(upload_id, None)

    job_id = await create_job(
        backend=settings.asr_backend,
        audio_path=audio_path,
        language=body.language,
        num_speakers=body.num_speakers,
        initial_prompt=body.initial_prompt,
        mode=mode,
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs")
async def get_jobs(mode: str | None = None) -> list[dict]:
    rows = await list_jobs(mode=mode)
    for row in rows:
        row["filename"] = Path(row["audio_path"]).name.split("_", 1)[-1]
    return rows


@app.get("/api/jobs/{job_id}")
async def get_one_job(job_id: str) -> dict:
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена")

    segments = None
    speakers = None
    if job.get("segments_json"):
        segments = json.loads(job["segments_json"])
        speakers = sorted({s["speaker"] for s in segments})

    speaker_names: dict[str, str] = {}
    if job.get("speaker_names_json"):
        speaker_names = json.loads(job["speaker_names_json"])

    return {
        "id": job["id"],
        "status": job["status"],
        "backend": job["backend"],
        "mode": job.get("mode") or "speakers",
        "progress": job["progress"],
        "stage": job["stage"],
        "error": job["error"],
        "filename": Path(job["audio_path"]).name.split("_", 1)[-1],
        "language": job["language"],
        "num_speakers": job["num_speakers"],
        "initial_prompt": job["initial_prompt"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "segments": segments,
        "speakers": speakers,
        "speaker_names": speaker_names,
        "has_output": bool(job.get("output_path")),
    }


@app.put("/api/jobs/{job_id}/speakers")
async def rename_speakers(job_id: str, mapping: dict[str, str]) -> dict[str, bool]:
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена")
    if job["status"] != JobStatus.DONE.value:
        raise HTTPException(400, "Задача ещё не завершена")
    if not job.get("output_path") or not job.get("segments_json"):
        raise HTTPException(400, "Нет данных для пересборки")

    await update_speaker_names(job_id, mapping)

    raw = json.loads(job["segments_json"])
    segments = [TranscriptSegment(**s) for s in raw]
    export_to_docx(segments, Path(job["output_path"]), speaker_names=mapping)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download")
async def download_docx(job_id: str) -> FileResponse:
    job = await get_job(job_id)
    if job is None or not job.get("output_path"):
        raise HTTPException(404, "Файл ещё не готов")
    path = Path(job["output_path"])
    if not path.exists():
        raise HTTPException(404, "Файл не найден на диске")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )


@app.delete("/api/jobs/{job_id}")
async def delete_one_job(job_id: str) -> dict[str, bool]:
    job = await get_job(job_id)
    if job is None:
        return {"ok": True}
    # Если задача сейчас обрабатывается — просим воркер прерваться на ближайшей
    # границе этапа, чтобы очередь не стояла.
    request_cancel(job_id)
    for key in ("audio_path", "output_path"):
        p = job.get(key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                # Файл может быть занят воркером (Windows) — не критично:
                # главное убрать задачу из очереди, файл подчистится позже.
                pass
    await delete_job(job_id)
    return {"ok": True}


# ---------- Voice profiles (Speaker Enrollment) ----------

@app.get("/api/profiles")
async def get_profiles() -> list[dict]:
    return await list_profiles()


@app.post("/api/profiles")
async def create_voice_profile(
    name: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    name = name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 200:
        raise HTTPException(400, "Имя слишком длинное")

    suffix = Path(file.filename or "audio").suffix.lower()
    if suffix and suffix not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            400,
            f"Формат {suffix} не поддерживается. "
            f"Поддерживаемые: {', '.join(sorted(ALLOWED_AUDIO_EXT))}",
        )

    profiles_dir = settings.storage_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    safe_name = Path(file.filename or "sample.wav").name
    sample_path = profiles_dir / f"{file_id}_{safe_name}"

    written = 0
    with sample_path.open("wb") as out:
        while True:
            chunk = await file.read(2 * 1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > 100 * 1024 * 1024:  # 100 MB лимит для образца
                out.close()
                sample_path.unlink(missing_ok=True)
                raise HTTPException(413, "Образец слишком большой (макс 100 MB)")
            out.write(chunk)
    if written == 0:
        sample_path.unlink(missing_ok=True)
        raise HTTPException(400, "Пустой файл")

    # Препроцессим образец и считаем embedding
    try:
        from server.audio_preprocess import preprocess

        work_path, is_tmp = await preprocess(sample_path, profiles_dir)
        try:
            extractor = EmbeddingExtractor.instance()
            embedding = await asyncio.to_thread(extractor.embed_whole, work_path)
        finally:
            if is_tmp and work_path != sample_path:
                work_path.unlink(missing_ok=True)
    except Exception as exc:
        sample_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Не удалось посчитать эмбеддинг: {exc}") from exc

    profile_id = await create_profile(name, embedding, sample_path)
    return {"id": profile_id, "name": name}


@app.delete("/api/profiles/{profile_id}")
async def delete_voice_profile(profile_id: str) -> dict:
    profile = await delete_profile(profile_id)
    if profile is None:
        return {"ok": True}
    sample = profile.get("sample_path")
    if sample:
        Path(sample).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/profiles/from-job/{job_id}")
async def create_profile_from_job(
    job_id: str,
    speaker_id: str = Form(...),
    name: str = Form(...),
) -> dict:
    """Сохранить голос конкретного SPEAKER_NN из готовой задачи как профиль."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")

    job = await get_job(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена")
    if job["status"] != JobStatus.DONE.value or not job.get("segments_json"):
        raise HTTPException(400, "Задача не завершена")

    audio = Path(job["audio_path"])
    if not audio.exists():
        raise HTTPException(404, "Исходное аудио задачи не найдено")

    raw_segments = json.loads(job["segments_json"])
    spk_segments = [
        (float(s["start"]), float(s["end"]))
        for s in raw_segments
        if s.get("speaker") == speaker_id
        and (float(s["end"]) - float(s["start"]))
            >= settings.voice_embedding_min_segment_seconds
    ]
    if not spk_segments:
        raise HTTPException(400, "Нет подходящих сегментов этого спикера")

    spk_segments.sort(key=lambda se: se[1] - se[0], reverse=True)
    top = spk_segments[: settings.voice_embedding_max_segments]

    from server.audio_preprocess import preprocess

    work_path, is_tmp = await preprocess(audio, settings.storage_dir / "uploads")
    try:
        extractor = EmbeddingExtractor.instance()
        embedding = await asyncio.to_thread(
            extractor.embed_segments, work_path, top
        )
    finally:
        if is_tmp and work_path != audio:
            work_path.unlink(missing_ok=True)

    if embedding is None:
        raise HTTPException(500, "Не удалось посчитать эмбеддинг из сегментов")

    profile_id = await create_profile(name, embedding, None)
    return {"id": profile_id, "name": name}


# ---------- Static / pages ----------

@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    # Дефолт — Classic (полный pyannote режим)
    return (CLIENT_DIR / "classic.html").read_text(encoding="utf-8")


@app.get("/classic", response_class=HTMLResponse)
async def classic_page() -> str:
    return (CLIENT_DIR / "classic.html").read_text(encoding="utf-8")


# Legacy aliases на Classic
@app.get("/pyannote", response_class=HTMLResponse)
async def pyannote_page_legacy() -> str:
    return (CLIENT_DIR / "classic.html").read_text(encoding="utf-8")


@app.get("/speakers", response_class=HTMLResponse)
async def speakers_page_legacy() -> str:
    return (CLIENT_DIR / "classic.html").read_text(encoding="utf-8")


@app.get("/text", response_class=HTMLResponse)
async def text_page() -> str:
    return (CLIENT_DIR / "text.html").read_text(encoding="utf-8")


@app.get("/nemo-sortformer", response_class=HTMLResponse)
async def nemo_sortformer_page() -> str:
    return (CLIENT_DIR / "nemo-sortformer.html").read_text(encoding="utf-8")


@app.get("/nemo-msdd", response_class=HTMLResponse)
async def nemo_msdd_page() -> str:
    return (CLIENT_DIR / "nemo-msdd.html").read_text(encoding="utf-8")


@app.get("/nemo", response_class=HTMLResponse)
async def nemo_page_legacy() -> str:
    # Legacy → теперь это Sortformer
    return (CLIENT_DIR / "nemo-sortformer.html").read_text(encoding="utf-8")


@app.get("/profiles", response_class=HTMLResponse)
async def profiles_page() -> str:
    return (CLIENT_DIR / "profiles.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
