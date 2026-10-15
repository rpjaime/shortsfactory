"""Cálculo de la línea de tiempo a partir de la duración REAL de cada audio.

Modelo (todo cuantizado a fotogramas para evitar derivas):

* La diapositiva *i* ocupa un «hueco» (``slot``) que empieza en ``T_i``.
* La transición de entrada de *i* ocurre en ``[T_i, T_i + t_in]`` y se solapa con la
  cola del clip anterior. El clip *i* dura ``slot_i + t_out_i``; así el solapamiento
  se cuenta UNA sola vez: duración total = Σ slot_i.
* La narración de *i* empieza en ``T_i + t_in + margen_previo`` y termina antes de
  ``T_{i+1} - pausa``: el audio nunca se corta ni se solapa con una transición.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TimelineItem:
    index: int
    kind: str                 # slide | outro
    slide_id: str
    image: str                # nombre de la imagen (o "" para la tarjeta de cierre)
    start: float              # T_i
    slot: float               # duración del hueco
    clip_duration: float      # slot + transición de salida
    transition_in: float
    transition_out: float
    narration_start: float | None
    narration_end: float | None
    audio_path: str | None
    audio_duration: float
    segments: list[dict[str, Any]] = field(default_factory=list)  # tiempos relativos al audio
    motion: str = "none"


@dataclass
class Timeline:
    fps: int
    total: float
    items: list[TimelineItem]
    transition: str
    warnings: list[str] = field(default_factory=list)
    target: float = 0.0
    narration_total: float = 0.0

    @property
    def total_frames(self) -> int:
        return int(round(self.total * self.fps))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def xfade_offsets(self) -> list[float]:
        """Offset de cada transición en la cadena xfade (= inicio de la diapositiva siguiente)."""
        return [it.start for it in self.items[1:]]


def _f(seconds: float, fps: int) -> int:
    return int(math.ceil(seconds * fps - 1e-6))


def build_timeline(entries: list[dict[str, Any]], *, fps: int, transition: str, transition_duration: float,
                   min_slide_duration: float, cover_duration: float, lead_margin: float,
                   pause_between: float, silent_duration: float, target_duration: float = 0.0,
                   fill_to_target: bool = False, video_fade: float = 0.0) -> Timeline:
    """Construye la línea de tiempo.

    ``entries``: [{kind, slide_id, image, audio_path|None, audio_duration, segments, motion}]
    """
    if not entries:
        raise ValueError("No hay diapositivas para la línea de tiempo.")
    if fps <= 0:
        raise ValueError("fps debe ser positivo.")
    warnings: list[str] = []
    n = len(entries)
    tr_frames = 0 if transition == "none" or n < 2 else max(1, int(round(transition_duration * fps)))
    lead = max(0.0, lead_margin)
    pause = max(0.0, pause_between)

    slots: list[int] = []
    for i, e in enumerate(entries):
        t_in = tr_frames if i > 0 else 0
        t_out = tr_frames if i < n - 1 else 0
        dur = float(e.get("audio_duration") or 0.0)
        if e.get("audio_path") and dur > 0:
            need = t_in / fps + lead + dur + pause
        else:
            need = t_in / fps + silent_duration
        if i == n - 1:
            need += max(0.0, video_fade)      # el fundido final nunca tapa la última frase
        minimum = max(min_slide_duration, cover_duration if i == 0 else 0.0)
        frames = max(_f(need, fps), _f(minimum, fps), t_in + t_out + 2)
        slots.append(frames)

    total_frames = sum(slots)
    if fill_to_target and target_duration > 0 and total_frames < target_duration * fps:
        extra = int(round(target_duration * fps)) - total_frames
        per = extra // n
        cap = 4 * fps
        for i in range(n):
            add = min(cap, per + (1 if i < extra - per * n else 0))
            slots[i] += add
        total_frames = sum(slots)

    items: list[TimelineItem] = []
    t = 0
    narr_total = 0.0
    for i, (e, s) in enumerate(zip(entries, slots)):
        t_in = tr_frames if i > 0 else 0
        t_out = tr_frames if i < n - 1 else 0
        dur = float(e.get("audio_duration") or 0.0)
        has_audio = bool(e.get("audio_path")) and dur > 0
        nstart = (t + t_in) / fps + lead if has_audio else None
        nend = nstart + dur if has_audio and nstart is not None else None
        if has_audio:
            narr_total += dur
        items.append(TimelineItem(
            index=i, kind=e.get("kind", "slide"), slide_id=e.get("slide_id", ""), image=e.get("image", ""),
            start=t / fps, slot=s / fps, clip_duration=(s + t_out) / fps,
            transition_in=t_in / fps, transition_out=t_out / fps,
            narration_start=round(nstart, 4) if nstart is not None else None,
            narration_end=round(nend, 4) if nend is not None else None,
            audio_path=e.get("audio_path") if has_audio else None, audio_duration=dur,
            segments=list(e.get("segments") or []), motion=e.get("motion", "none"),
        ))
        t += s
    total = total_frames / fps

    if target_duration > 0 and total > target_duration * 1.10:
        warnings.append(
            f"La duración calculada ({total:.1f} s) supera la objetivo ({target_duration:.0f} s). "
            "Opciones: ampliar la duración objetivo, acortar el guion o regenerar la narración con un ritmo "
            "ligeramente mayor (sin pasar de 1,15×). El audio nunca se recorta.")
    if total > 60.0:
        warnings.append(f"El vídeo dura {total:.1f} s: YouTube Shorts admite hasta 3 minutos, pero los Shorts "
                        "más eficaces suelen durar menos de 60 s.")
    return Timeline(fps=fps, total=total, items=items, transition=transition if tr_frames else "none",
                    warnings=warnings, target=target_duration, narration_total=narr_total)


def validate_timeline(tl: Timeline) -> list[str]:
    """Comprobaciones de coherencia (usadas en pruebas y antes de renderizar)."""
    errs = []
    expected = 0.0
    for it in tl.items:
        if abs(it.start - expected) > 1e-6:
            errs.append(f"Hueco {it.index}: inicio {it.start} ≠ {expected}")
        expected += it.slot
        if it.narration_end is not None:
            nxt_tr = it.start + it.slot
            if it.narration_end > nxt_tr + 1e-6:
                errs.append(f"Hueco {it.index}: la narración termina después de la transición siguiente")
            if it.narration_start is not None and it.narration_start < it.start + it.transition_in - 1e-6:
                errs.append(f"Hueco {it.index}: la narración empieza durante la transición de entrada")
    if abs(expected - tl.total) > 1e-6:
        errs.append(f"Total {tl.total} ≠ suma de huecos {expected}")
    return errs
