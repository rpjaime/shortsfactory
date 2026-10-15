"""Validaciones de entrada (antes de renderizar) y de salida (con ffprobe)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Settings, ffmpeg_exe, ffprobe_exe, get_settings

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_markdown(self, title: str = "") -> str:
        out = [f"**{title}**"] if title else []
        out += [f"- ❌ {e}" for e in self.errors]
        out += [f"- ⚠️ {w}" for w in self.warnings]
        out += [f"- ✅ {i}" for i in self.info]
        return "\n".join(out) if out else "Sin incidencias."

    def to_dict(self) -> dict[str, Any]:
        return {"errors": self.errors, "warnings": self.warnings, "info": self.info}


# ---------------------------------------------------------------------------
# FFmpeg
# ---------------------------------------------------------------------------

@dataclass
class FFmpegInfo:
    ffmpeg: str | None
    ffprobe: str | None
    version: str = ""
    major: int = 0
    has_libx264: bool = False
    has_aac: bool = False
    has_ass: bool = False
    has_xfade: bool = False
    has_sidechain: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe and self.has_libx264 and self.has_aac and self.has_ass
                    and self.has_xfade and self.has_sidechain and not self.error)

    def problems(self) -> list[str]:
        p = []
        if not self.ffmpeg:
            p.append("No se encuentra FFmpeg. Instálalo (por ejemplo «winget install Gyan.FFmpeg») o indica su "
                     "ruta en Ajustes.")
        if not self.ffprobe:
            p.append("No se encuentra ffprobe (se instala junto a FFmpeg).")
        if self.ffmpeg and not self.error:
            for ok, what in ((self.has_libx264, "el codificador libx264"), (self.has_aac, "el codificador AAC"),
                             (self.has_ass, "el filtro «ass» (libass) para subtítulos"),
                             (self.has_xfade, "el filtro «xfade» (FFmpeg ≥ 4.3)"),
                             (self.has_sidechain, "el filtro «sidechaincompress»")):
                if not ok:
                    p.append(f"Tu FFmpeg no incluye {what}. Usa una compilación completa (p. ej. gyan.dev «full»).")
        if self.error:
            p.append(self.error)
        return p


def _run(args: list[str], timeout: float = 20) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)
    return (proc.stdout or "") + (proc.stderr or "")


def inspect_ffmpeg(settings: Settings | None = None) -> FFmpegInfo:
    s = settings or get_settings()
    info = FFmpegInfo(ffmpeg_exe(s), ffprobe_exe(s))
    if not info.ffmpeg:
        return info
    try:
        out = _run([info.ffmpeg, "-hide_banner", "-version"])
        m = re.search(r"ffmpeg version (\S+)", out)
        info.version = m.group(1) if m else "desconocida"
        mm = re.match(r"n?(\d+)\.", info.version)
        info.major = int(mm.group(1)) if mm else 0
        enc = _run([info.ffmpeg, "-hide_banner", "-encoders"])
        info.has_libx264 = " libx264 " in enc
        info.has_aac = re.search(r"\saac\s", enc) is not None
        flt = _run([info.ffmpeg, "-hide_banner", "-filters"])
        info.has_ass = re.search(r"\sass\s", flt) is not None
        info.has_xfade = " xfade " in flt
        info.has_sidechain = " sidechaincompress " in flt
    except (OSError, subprocess.TimeoutExpired) as exc:
        info.error = f"FFmpeg no se pudo ejecutar: {exc}"
    return info


def ffprobe_json(path: Path, ffprobe: str) -> dict[str, Any]:
    proc = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                           str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe no pudo leer {Path(path).name}: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "{}")


def media_duration(path: Path, ffprobe: str) -> float:
    data = ffprobe_json(path, ffprobe)
    try:
        return float(data["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Antes de renderizar
# ---------------------------------------------------------------------------

def free_space_gb(path: Path) -> float:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def preflight(project, ff: FFmpegInfo, *, preview: bool, voice_ok: tuple[bool, str],
              music_path: Path | None, font_path: Path | None, estimated_duration: float | None = None,
              settings: Settings | None = None) -> CheckReport:
    from fonts import supports_spanish
    from image_processor import analyze_image
    from script_manager import validate_project_script

    s = settings or get_settings()
    rep = CheckReport()
    slides = project.enabled_slides()
    valid = 0
    for sl in slides:
        try:
            p = project.image_path(sl["image"])
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(str(exc))
            continue
        if not p.is_file():
            rep.errors.append(f"No se puede acceder a la imagen {sl['image']} ({p}).")
            continue
        info = analyze_image(p)
        if not info.ok:
            rep.errors.append(f"{sl['image']}: {info.error}")
        else:
            valid += 1
            rep.warnings += [f"{sl['image']}: {w}" for w in info.warnings if "pequeña" in w]
    if valid == 0:
        rep.errors.append("No hay ninguna imagen válida y activa.")
    else:
        rep.info.append(f"{valid} imagen(es) válida(s) y accesible(s).")

    script = validate_project_script(project)
    rep.errors += script.errors
    rep.warnings += script.warnings
    if script.ok:
        rep.info.append("Guion completo para las diapositivas que requieren narración.")

    ok, msg = voice_ok
    if ok:
        rep.info.append(f"Voz instalada: {project.data['voice'].get('voice_id')}.")
    else:
        rep.errors.append(msg)

    rep.errors += ff.problems()
    if ff.ok:
        rep.info.append(f"FFmpeg {ff.version} operativo (libx264, AAC, libass, xfade).")

    need_gb = 0.5 if preview else 2.0
    for label, path in (("salida", s.outputs_path), ("temporales", s.temp_path), ("caché", s.cache_path)):
        try:
            free = free_space_gb(path)
            if free < need_gb:
                rep.errors.append(f"Espacio libre insuficiente en la unidad de {label}: {free:.1f} GB "
                                  f"(se recomiendan {need_gb:.1f} GB).")
        except OSError as exc:
            rep.warnings.append(f"No se pudo comprobar el espacio libre de {label}: {exc}")

    if music_path is not None:
        if not music_path.is_file():
            rep.errors.append(f"No se encuentra la música seleccionada: {music_path}")
        elif ff.ffprobe:
            try:
                dur = media_duration(music_path, ff.ffprobe)
                data = ffprobe_json(music_path, ff.ffprobe)
                if not any(st.get("codec_type") == "audio" for st in data.get("streams", [])) or dur <= 0:
                    rep.errors.append(f"La música {music_path.name} no contiene audio legible.")
                else:
                    rep.info.append(f"Música legible ({dur:.1f} s); se repetirá si es más corta que el vídeo.")
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(f"La música {music_path.name} no es legible: {exc}")

    if font_path is None:
        rep.errors.append("No hay ninguna tipografía TTF/OTF disponible para los subtítulos.")
    elif not supports_spanish(font_path):
        rep.warnings.append(f"La fuente {font_path.name} no parece incluir todos los acentos y signos españoles.")
    else:
        rep.info.append(f"Subtítulos generables con la fuente {font_path.name}.")

    if estimated_duration is not None:
        if estimated_duration < 3:
            rep.errors.append(f"Duración calculada demasiado corta ({estimated_duration:.1f} s).")
        elif estimated_duration > 180:
            rep.errors.append(f"Duración calculada ({estimated_duration:.0f} s) superior a 3 minutos: "
                              "no es un Short.")
    return rep


# ---------------------------------------------------------------------------
# Después de renderizar
# ---------------------------------------------------------------------------

def postflight(mp4: Path, ff: FFmpegInfo, *, width: int, height: int, expected_duration: float,
               narration_end: float, srt_path: Path | None) -> CheckReport:
    rep = CheckReport()
    if not mp4.is_file():
        rep.errors.append("El MP4 no existe.")
        return rep
    size = mp4.stat().st_size
    if size <= 0:
        rep.errors.append("El MP4 tiene tamaño cero.")
        return rep
    rep.info.append(f"MP4 creado ({size / 1e6:.1f} MB).")
    try:
        data = ffprobe_json(mp4, ff.ffprobe or "ffprobe")
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"El archivo no es reproducible: {exc}")
        return rep
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        rep.errors.append("El MP4 no tiene pista de vídeo.")
    else:
        if (v.get("width"), v.get("height")) != (width, height):
            rep.errors.append(f"Dimensiones {v.get('width')}×{v.get('height')} ≠ {width}×{height}.")
        else:
            rep.info.append(f"Dimensiones correctas: {width}×{height} (9:16).")
        if v.get("codec_name") != "h264":
            rep.errors.append(f"Códec de vídeo {v.get('codec_name')} (se esperaba H.264).")
        if v.get("pix_fmt") != "yuv420p":
            rep.errors.append(f"Formato de píxel {v.get('pix_fmt')} (se esperaba yuv420p).")
        else:
            rep.info.append("H.264 · yuv420p.")
    if a is None:
        rep.errors.append("El MP4 no tiene pista de audio.")
    else:
        rep.info.append(f"Pista de audio {a.get('codec_name')} a {a.get('sample_rate')} Hz.")
        if a.get("codec_name") != "aac":
            rep.errors.append(f"Códec de audio {a.get('codec_name')} (se esperaba AAC).")
    try:
        dur = float(data["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        dur = 0.0
    if abs(dur - expected_duration) > 0.25:
        rep.errors.append(f"Duración {dur:.2f} s distinta de la calculada ({expected_duration:.2f} s).")
    else:
        rep.info.append(f"Duración {dur:.2f} s coherente con el audio y la línea de tiempo.")
    if dur + 0.05 < narration_end:
        rep.errors.append(f"El vídeo ({dur:.2f} s) termina antes que la narración ({narration_end:.2f} s).")
    else:
        rep.info.append("El vídeo no termina antes que la narración.")
    if srt_path is None or not srt_path.is_file() or srt_path.stat().st_size == 0:
        rep.errors.append("No se ha generado el archivo SRT.")
    else:
        rep.info.append(f"Subtítulos SRT generados: {srt_path.name}.")
    return rep
