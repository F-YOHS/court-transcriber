from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    asr_backend: Literal["whisperx", "yandex", "mock"] = "whisperx"

    whisper_model: str = "large-v3"
    whisper_device: Literal["cuda", "cpu"] = "cuda"
    whisper_compute_type: Literal["float16", "int8", "float32"] = "float16"
    whisper_language: str = "ru"
    # Размер батча Whisper. Больше = быстрее, но больше VRAM. 16 — для скорости;
    # если поймаешь CUDA OOM — снизь до 8 (между задачами VRAM теперь чистится).
    whisper_batch_size: int = 16

    # Батч для pyannote (сегментация + эмбеддинги). По умолчанию pyannote
    # использует маленький батч и недогружает GPU → медленно на длинных
    # записях. 64 заметно ускоряет; если CUDA OOM на диаризации — снизь до 32.
    diarization_batch_size: int = 64
    hf_token: str = ""

    # Какую модель диаризации использовать. Пустая строка = дефолт whisperx
    # (на 3.8 это community-1). Можно переключить на конкретную модель,
    # например pyannote/speaker-diarization-3.1 — сравнить качество.
    diarization_model: str = ""

    # Какой диаризационный бэкенд по умолчанию (можно переопределить per-job из UI):
    #   auto             — NeMo Sortformer если установлен, fallback на pyannote
    #   nemo-sortformer  — только NeMo Sortformer (до 4 спикеров, e2e transformer)
    #   nemo-msdd        — только NeMo MSDD (много спикеров, кластеризация)
    #   pyannote         — только pyannote (Classic, через whisperx)
    #   nemo             — legacy alias для nemo-sortformer
    diarization_backend: Literal[
        "auto", "nemo", "nemo-sortformer", "nemo-msdd", "pyannote"
    ] = "auto"

    # Модель NeMo Sortformer (до 4 спикеров, fixed).
    nemo_diarization_model: str = "nvidia/diar_sortformer_4spk-v1"

    # Модель NeMo MSDD (поддерживает много спикеров). Имя идёт БЕЗ префикса
    # 'nvidia/': MSDD берётся из NGC-реестра NeMo, а не с HuggingFace
    # (с префиксом 'nvidia/...' получаем 404 — такого HF-репо нет).
    # Варианты: diar_msdd_telephonic, diar_msdd_meeting
    nemo_msdd_model: str = "diar_msdd_telephonic"

    # Включать ли ffmpeg-препроцессинг (denoise + normalize + 16kHz mono).
    # По умолчанию ВЫКЛ — пользователь явно отказался от «обрезки» аудио.
    audio_preprocess: bool = False
    # Агрессивный денойзинг (afftdn) — может срезать тихую речь, по умолчанию выкл
    audio_preprocess_aggressive: bool = False
    # Сколько секунд паузы максимум для слияния соседних реплик одного спикера
    merge_max_gap_seconds: float = 0.8

    # Если пауза между сегментами длиннее этого порога — добавляем
    # синтетический сегмент "[пауза N сек]" в результат.
    silence_pause_threshold_seconds: float = 30.0

    # --- Voice profiles (Speaker Enrollment) ---
    # Модель для речевых эмбеддингов. wespeaker-voxceleb-resnet34-LM — публичная.
    embedding_model: str = "pyannote/wespeaker-voxceleb-resnet34-LM"
    # Порог cosine similarity для автоматического матчинга со спикером в профиле.
    # 0.7 — консервативно. Ниже = больше совпадений (риск ложных), выше = меньше (риск пропустить).
    voice_match_threshold: float = 0.65
    # Максимум сегментов одного спикера, которые усредняем в его embedding.
    # Меньше — быстрее, обычно достаточно 5-8 чистых сегментов.
    voice_embedding_max_segments: int = 8
    # Минимальная длина сегмента (сек), который годится для вычисления embedding.
    voice_embedding_min_segment_seconds: float = 1.5

    yandex_api_key: str = ""
    yandex_folder_id: str = ""

    host: str = "0.0.0.0"
    port: int = 8000
    storage_dir: Path = Field(default=Path("./storage"))
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    auth_username: str = "mom"
    auth_password_hash: str = ""
    jwt_secret: str = ""
    jwt_ttl_hours: int = 24

    queue_db_path: Path = Field(default=Path("./storage/queue.sqlite"))
    worker_concurrency: int = 1


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
