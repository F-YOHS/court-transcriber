"""
Извлечение голосовых эмбеддингов (256-dim) через pyannote.

Используется в двух местах:
  1) Создание профиля голоса по образцу (целый файл → один эмбеддинг)
  2) Идентификация спикера в задаче (несколько сегментов → усреднённый эмбеддинг)

Модель загружается лениво при первом обращении.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from server.config import settings


class EmbeddingExtractor:
    _instance: "EmbeddingExtractor | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._whole_inference = None
        self._loaded_event = threading.Event()

    @classmethod
    def instance(cls) -> "EmbeddingExtractor":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_loaded(self) -> None:
        if self._loaded_event.is_set():
            return
        with self._lock:
            if self._loaded_event.is_set():
                return
            if not settings.hf_token:
                raise RuntimeError(
                    "HF_TOKEN не задан — без него pyannote не скачает модель эмбеддингов."
                )
            from pyannote.audio import Inference, Model

            model = Model.from_pretrained(
                settings.embedding_model,
                use_auth_token=settings.hf_token,
            )
            try:
                model.to(_torch_device())
            except Exception:
                pass
            self._model = model
            self._whole_inference = Inference(model, window="whole")
            self._loaded_event.set()

    def embed_whole(self, audio_path: Path) -> np.ndarray:
        """Эмбеддинг целого файла (для образца профиля)."""
        self._ensure_loaded()
        result = self._whole_inference(str(audio_path))
        return _as_vector(result)

    def embed_segments(
        self,
        audio_path: Path,
        segments: list[tuple[float, float]],
    ) -> np.ndarray | None:
        """
        Среднее по эмбеддингам списка сегментов.
        None — если ни один сегмент не дал валидного эмбеддинга.
        """
        if not segments:
            return None
        self._ensure_loaded()
        from pyannote.audio import Inference
        from pyannote.core import Segment

        crop_inference = Inference(self._model, window="whole")
        vectors: list[np.ndarray] = []
        for start, end in segments:
            try:
                seg_result = crop_inference.crop(
                    str(audio_path), Segment(start=start, end=end)
                )
                vec = _as_vector(seg_result)
                if vec is not None and np.isfinite(vec).all():
                    vectors.append(vec)
            except Exception:
                continue
        if not vectors:
            return None
        mean = np.mean(np.stack(vectors), axis=0)
        return _normalize(mean)


def _torch_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _as_vector(result) -> np.ndarray | None:
    if result is None:
        return None
    data = getattr(result, "data", result)
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=0)
    if arr.ndim != 1:
        return None
    return _normalize(arr)


def _normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    if norm < 1e-9:
        return v
    return v / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Оба вектора уже нормализованы — просто dot product."""
    return float(np.dot(a, b))
