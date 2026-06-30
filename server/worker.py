import asyncio
import json
from pathlib import Path

from server.audio_preprocess import preprocess, to_mono_16k_sync
from server.backends import get_backend
from server.config import settings
from server.export import export_to_docx
from server.queue import (
    JobCanceled,
    complete_job,
    is_cancel_requested,
    update_progress,
    update_speaker_names,
)
from server.silence import annotate_pauses
from server.voice_matching import match_speakers_to_profiles


async def process_job(job: dict) -> None:
    backend = get_backend()
    audio_path = Path(job["audio_path"])
    loop = asyncio.get_running_loop()

    def _progress(value: float, stage: str) -> None:
        # На границе каждого этапа проверяем, не отменил ли пользователь задачу.
        # Если да — прерываем обработку (освобождаем GPU и очередь).
        if is_cancel_requested(job["id"]):
            raise JobCanceled()
        asyncio.run_coroutine_threadsafe(
            update_progress(job["id"], value, stage), loop
        )

    raw_mode = (job.get("mode") or "classic").lower()
    # Legacy aliases
    if raw_mode in ("speakers", "pyannote"):
        raw_mode = "classic"
    elif raw_mode == "nemo":
        raw_mode = "nemo-sortformer"

    if raw_mode == "text_only":
        with_diarization = False
        diar_backend_name: str | None = None
    elif raw_mode == "nemo-sortformer":
        with_diarization = True
        diar_backend_name = "nemo-sortformer"
    elif raw_mode == "nemo-msdd":
        with_diarization = True
        diar_backend_name = "nemo-msdd"
    else:  # classic — дефолт
        with_diarization = True
        diar_backend_name = "pyannote"

    # Максимум фич во всех режимах со спикерами — voting/merge/profiles везде.
    apply_features = with_diarization

    # Препроцессинг — ВЫКЛ во всех режимах (пользователь не хочет «обрезки»).
    # Если кто-то захочет вернуть — есть AUDIO_PREPROCESS в .env, но он
    # подключается только если явно включён.
    work_audio = audio_path
    tmp_audio: Path | None = None
    if settings.audio_preprocess:
        _progress(0.02, "Подготовка аудиозаписи")
        work_audio, is_tmp = await preprocess(
            audio_path, settings.storage_dir / "uploads"
        )
        if is_tmp:
            tmp_audio = work_audio
    elif with_diarization:
        # Диаризация (NeMo/pyannote) и матчинг по голосу читают файл сами и
        # падают на стерео-записи (NeMo VAD: torch.cat 1D+2D). Приводим к
        # 16 кГц моно — без фильтров, ничего не «обрезаем».
        _progress(0.02, "Подготовка аудиозаписи")
        work_audio, is_tmp = await asyncio.to_thread(
            to_mono_16k_sync, audio_path, settings.storage_dir / "uploads"
        )
        if is_tmp:
            tmp_audio = work_audio

    try:
        segments = await backend.transcribe(
            work_audio,
            language=job["language"],
            num_speakers=job["num_speakers"],
            initial_prompt=job["initial_prompt"],
            progress=_progress,
            with_diarization=with_diarization,
            diarization_backend=diar_backend_name,
            apply_post_processing=apply_features,
        )

        # Speaker Enrollment — во всех режимах со спикерами.
        auto_names: dict[str, str] = {}
        if apply_features:
            try:
                _progress(0.97, "Идентификация по образцам голоса")
                auto_names = await match_speakers_to_profiles(work_audio, segments)
            except Exception:
                auto_names = {}
    finally:
        if tmp_audio is not None:
            tmp_audio.unlink(missing_ok=True)

    # Пометки тишины — в обоих режимах
    segments = annotate_pauses(segments)

    output_dir = settings.storage_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = audio_path.stem.split("_", 1)[-1] or job["id"]
    # Суффикс job_id, чтобы две записи с одинаковым именем файла (диктофоны
    # часто дают REC001.WAV и т.п.) не перезаписывали .docx друг друга на диске.
    output_path = output_dir / f"{base_name}_{job['id'][:8]}.docx"

    speaker_names: dict[str, str] = {}
    if job.get("speaker_names_json"):
        speaker_names = json.loads(job["speaker_names_json"])
    for spk_id, name in auto_names.items():
        speaker_names.setdefault(spk_id, name)

    if auto_names:
        await update_speaker_names(job["id"], speaker_names)

    export_to_docx(
        segments,
        output_path,
        speaker_names=speaker_names,
        show_speakers=with_diarization,
    )
    # для логирования режима в БД (опционально, не критично)
    _ = (raw_mode, diar_backend_name)

    await complete_job(
        job["id"],
        segments=[
            {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
            for s in segments
        ],
        output_path=output_path,
    )
