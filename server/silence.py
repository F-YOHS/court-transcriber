"""
Расстановка псевдо-сегментов «[пауза N сек]» в тех местах, где между
соседними репликами зазор превышает порог.
"""
from __future__ import annotations

from server.backends.base import TranscriptSegment
from server.config import settings

PAUSE_SPEAKER = "PAUSE"


def _human_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total} сек"
    minutes = total // 60
    secs = total % 60
    if secs == 0:
        return f"{minutes} мин"
    return f"{minutes} мин {secs} сек"


def annotate_pauses(
    segments: list[TranscriptSegment],
    *,
    threshold_seconds: float | None = None,
) -> list[TranscriptSegment]:
    threshold = (
        threshold_seconds
        if threshold_seconds is not None
        else settings.silence_pause_threshold_seconds
    )
    if not segments or threshold <= 0:
        return segments

    result: list[TranscriptSegment] = []
    prev_end: float | None = None
    for seg in segments:
        if prev_end is not None:
            gap = seg.start - prev_end
            if gap >= threshold:
                result.append(
                    TranscriptSegment(
                        start=prev_end,
                        end=seg.start,
                        text=f"[пауза {_human_duration(gap)}]",
                        speaker=PAUSE_SPEAKER,
                    )
                )
        result.append(seg)
        prev_end = seg.end
    return result
