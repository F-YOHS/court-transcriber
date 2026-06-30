"""
Препроцессинг аудио перед ASR/диаризацией.

Щадящий режим (по умолчанию):
  - highpass 80 Hz, lowpass 8000 Hz — режут гул/шипение по краям спектра речи
  - dynaudnorm — поднимает тихие участки БЕЗ давления громких (тихий свидетель станет слышнее)
  - ресэмплинг в 16 kHz mono WAV

Агрессивный режим (AUDIO_PREPROCESS_AGGRESSIVE=true):
  - дополнительно FFT-денойзер afftdn — может срезать речь в шумных местах,
    но даёт более чистый сигнал.

Если ffmpeg недоступен — возвращает исходный путь, не падает.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from server.config import settings


def _filter_chain(aggressive: bool) -> str:
    parts = [
        "highpass=f=80",
        "lowpass=f=8000",
    ]
    if aggressive:
        parts.append("afftdn=nr=10")
    # dynaudnorm: f=frame_ms, g=gauss, p=peak_target, m=max_gain
    # Мягкие настройки — поднимаем тихих, не пампая громких.
    parts.append("dynaudnorm=f=200:g=15:p=0.95:m=15")
    return ",".join(parts)


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


async def preprocess(input_path: Path, output_dir: Path) -> tuple[Path, bool]:
    """
    Возвращает (output_path, is_temp).
    Если is_temp == True — вызывающему стоит удалить output_path после использования.
    Если ffmpeg недоступен или фильтры упали — возвращает (input_path, False).
    """
    if not has_ffmpeg():
        return input_path, False

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{input_path.stem}__preprocessed.wav"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-i", str(input_path),
        "-af", _filter_chain(settings.audio_preprocess_aggressive),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]

    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True
    )
    if result.returncode != 0 or not output_path.exists():
        output_path.unlink(missing_ok=True)
        return input_path, False
    return output_path, True


def to_mono_16k_sync(input_path: Path, output_dir: Path) -> tuple[Path, bool]:
    """Чистое приведение к 16 кГц моно WAV — БЕЗ фильтров/шумодава.

    Нужно бэкендам, которые читают файл сами и падают на стерео-записи
    (NeMo VAD: torch.cat 1D+2D на 2-канальном сигнале). Это НЕ «обрезка»:
    меняются только число каналов и частота дискретизации — формат, который
    модели диаризации и так требуют. Возвращает (path, is_temp);
    если ffmpeg недоступен — (input_path, False).
    """
    if not has_ffmpeg():
        return input_path, False

    output_dir.mkdir(parents=True, exist_ok=True)
    # Имя без пробелов/кириллицы: NeMo использует имя файла как session-id и
    # ломается на разборе RTTM, если в имени есть пробелы
    # (ValueError: could not convert string to float: 'сз').
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", input_path.stem)[:40].strip("._-") or "audio"
    output_path = output_dir / f"{safe_stem}_{uuid.uuid4().hex[:8]}_mono16k.wav"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-i", str(input_path),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        output_path.unlink(missing_ok=True)
        return input_path, False
    return output_path, True
