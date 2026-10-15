"""Mezcla de narración, silencios y música con FFmpeg.

* Cada fragmento de voz se coloca en su instante exacto (``adelay``) de la línea de tiempo.
* La música (opcional) se repite si es corta, se recorta a la duración total, lleva
  fundidos de entrada/salida y se atenúa automáticamente cuando habla la voz
  (``sidechaincompress``).
* Un limitador final evita el clipping. Después se mide el resultado para comprobar
  que no está vacío ni satura.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from timeline import Timeline

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
LIMIT_LINEAR = 0.891   # -1 dBFS


class AudioError(Exception):
    pass


@dataclass
class MixStats:
    mean_db: float
    peak_db: float

    @property
    def silent(self) -> bool:
        return self.mean_db <= -70.0

    @property
    def clipping(self) -> bool:
        return self.peak_db > -0.1


def build_mix_command(ffmpeg: str, timeline: Timeline, audio_cfg: dict[str, Any], out_wav: Path,
                      music_path: Path | None = None) -> list[str]:
    """Devuelve la lista de argumentos de FFmpeg (sin concatenar cadenas de shell)."""
    total = timeline.total
    voice_items = [it for it in timeline.items if it.audio_path and it.narration_start is not None]
    args: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for it in voice_items:
        args += ["-i", str(it.audio_path)]
    use_music = music_path is not None
    if use_music:
        args += ["-stream_loop", "-1", "-i", str(music_path)]

    vol_voice = float(audio_cfg.get("voice_volume", 1.0))
    parts: list[str] = []
    labels = []
    for k, it in enumerate(voice_items):
        delay_ms = int(round(it.narration_start * 1000))
        parts.append(
            f"[{k}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=mono,"
            f"adelay={delay_ms}:all=1[n{k}]")
        labels.append(f"[n{k}]")
    if labels:
        if len(labels) == 1:
            parts.append(f"{labels[0]}anull[vsum]")
        else:
            parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest[vsum]")
        parts.append(f"[vsum]volume={vol_voice:.3f},apad=whole_dur={total:.3f},atrim=0:{total:.3f},"
                     f"pan=stereo|c0=c0|c1=c0[voice]")
    else:
        parts.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=0:{total:.3f}[voice]")

    if use_music:
        m = len(voice_items)
        mv = float(audio_cfg.get("music_volume", 0.18))
        fi = max(0.0, float(audio_cfg.get("fade_in", 1.0)))
        fo = max(0.0, min(float(audio_cfg.get("fade_out", 1.5)), total / 2))
        parts.append(
            f"[{m}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={mv:.3f}"
            + (f",afade=t=in:st=0:d={fi:.3f}" if fi > 0 else "")
            + (f",afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.3f}" if fo > 0 else "")
            + "[mus]")
        if audio_cfg.get("ducking", True) and voice_items:
            strength = min(1.0, max(0.0, float(audio_cfg.get("ducking_strength", 0.6))))
            ratio = 2 + 10 * strength
            parts.append("[voice]asplit=2[vmain][vkey]")
            parts.append(f"[mus][vkey]sidechaincompress=threshold=0.02:ratio={ratio:.1f}:attack=15:release=350:"
                         f"makeup=1[ducked]")
            parts.append("[vmain][ducked]amix=inputs=2:normalize=0:duration=first[mixed]")
        else:
            parts.append("[voice][mus]amix=inputs=2:normalize=0:duration=first[mixed]")
    else:
        parts.append("[voice]anull[mixed]")
    parts.append(f"[mixed]alimiter=limit={LIMIT_LINEAR}:level=disabled,atrim=0:{total:.3f}[out]")

    args += ["-filter_complex", ";".join(parts), "-map", "[out]", "-ar", str(SAMPLE_RATE),
             "-c:a", "pcm_s16le", "-t", f"{total:.3f}", str(out_wav)]
    return args


def run_ffmpeg(args: list[str], what: str) -> None:
    log.debug("FFmpeg (%s): %s", what, args)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        raise AudioError(f"No se encuentra FFmpeg para {what}.") from exc
    if proc.returncode != 0:
        raise AudioError(f"FFmpeg falló durante {what}:\n{proc.stderr.strip()[-1500:]}")


def measure(ffmpeg: str, wav: Path) -> MixStats:
    args = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", proc.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf)", proc.stderr)
    to_f = lambda m: -999.0 if (m is None or m.group(1) == "-inf") else float(m.group(1))
    return MixStats(to_f(mean), to_f(peak))


def mix(ffmpeg: str, timeline: Timeline, audio_cfg: dict[str, Any], out_wav: Path,
        music_path: Path | None = None) -> MixStats:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(build_mix_command(ffmpeg, timeline, audio_cfg, out_wav, music_path), "la mezcla de audio")
    stats = measure(ffmpeg, out_wav)
    if any(it.audio_path for it in timeline.items) and stats.silent:
        raise AudioError("La mezcla de audio ha quedado en silencio. Revisa los audios de narración.")
    if stats.clipping:
        log.warning("La mezcla roza el máximo (pico %.1f dB).", stats.peak_db)
    return stats
