"""
Сопоставление спикеров в задаче с профилями голосов.

Алгоритм:
  1. Сгруппировать сегменты по spk_id (SPEAKER_00, SPEAKER_01, ...)
  2. Для каждого spk_id взять top-N длинных сегментов
  3. Посчитать средний нормализованный embedding
  4. Сравнить с каждым профилем через cosine similarity
  5. Если max similarity > порога — это match, иначе оставить SPEAKER_NN
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np

from server.backends.base import TranscriptSegment
from server.config import settings
from server.embedding import EmbeddingExtractor, cosine_similarity
from server.voice_profiles import all_embeddings


async def match_speakers_to_profiles(
    audio_path: Path,
    segments: list[TranscriptSegment],
) -> dict[str, str]:
    """
    Возвращает mapping {SPEAKER_NN: "Имя из профиля"} только для совпавших.
    Не совпавшие в результате не появляются — мама их сама переименует.
    """
    profiles = await all_embeddings()
    if not profiles:
        return {}

    by_speaker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for seg in segments:
        dur = seg.end - seg.start
        if dur >= settings.voice_embedding_min_segment_seconds:
            by_speaker[seg.speaker].append((seg.start, seg.end))

    if not by_speaker:
        return {}

    extractor = EmbeddingExtractor.instance()
    mapping: dict[str, str] = {}

    for spk_id, spk_segments in by_speaker.items():
        spk_segments.sort(key=lambda se: se[1] - se[0], reverse=True)
        top = spk_segments[: settings.voice_embedding_max_segments]
        emb = extractor.embed_segments(audio_path, top)
        if emb is None:
            continue
        best_name: str | None = None
        best_score = -1.0
        for _pid, name, prof_vec in profiles:
            score = cosine_similarity(emb, prof_vec)
            if score > best_score:
                best_score = score
                best_name = name
        if best_name is not None and best_score >= settings.voice_match_threshold:
            mapping[spk_id] = best_name
    return mapping
