"""Renderizado y composición de vídeo con FFmpeg.

Flujo de ``render_project``:

1. Validaciones previas (``quality_checks.preflight``).
2. Narración por fragmentos con caché (``tts_engine``).
3. Línea de tiempo a partir de las duraciones reales (``timeline``).
4. Subtítulos ASS + SRT (``subtitle_engine``).
5. Un clip por diapositiva (reutilizado de la caché si no ha cambiado).
6. Mezcla de audio (``audio_mixer``).
7. Montaje final: transiciones ``xfade``, fundidos, subtítulos ``ass``, H.264 + AAC.
8. Verificación del MP4 con ffprobe (``quality_checks.postflight``).

Todos los comandos de FFmpeg se construyen como listas de argumentos.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image

import audio_mixer
from config import PREVIEW_HEIGHT, PREVIEW_WIDTH, Settings, get_settings
from fonts import font_family_name, resolve_font_path
from image_processor import (Composition, compose_slide, load_image, make_outro_card, motion_params,
                             render_frame, resolve_motion)
from jobs import Cancelled, Job
from project_manager import Project, slugify
from quality_checks import FFmpegInfo, inspect_ffmpeg, postflight, preflight
from script_manager import normalize_text, validate_project_script
from subtitle_engine import build_cues, sample_cues, write_ass, write_srt
from timeline import Timeline, build_timeline, validate_timeline
from pronunciation import load_lexicon
from tts_engine import NarrationCache, PiperEngine, TTSEngine, TTSError, VoiceParams, get_engine

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RENDER_VERSION = "2"
_render_lock = threading.Lock()   # un único renderizado pesado a la vez


class RenderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parámetros de salida
# ---------------------------------------------------------------------------

@dataclass
class OutputSpec:
    width: int
    height: int
    fps: int
    crf: int
    preset: str
    audio_bitrate: str
    preview: bool

    @classmethod
    def from_project(cls, project: Project, preview: bool) -> "OutputSpec":
        e = project.data["export"]
        if preview:
            return cls(PREVIEW_WIDTH, PREVIEW_HEIGHT, int(e.get("fps", 30)), 28, "veryfast", "128k", True)
        w, h = int(e.get("width", 1080)), int(e.get("height", 1920))
        w -= w % 2
        h -= h % 2
        return cls(w, h, int(e.get("fps", 30)), int(e.get("crf", 20)), str(e.get("preset", "medium")),
                   str(e.get("audio_bitrate", "192k")), False)


# ---------------------------------------------------------------------------
# Ejecución de FFmpeg con progreso real y cancelación
# ---------------------------------------------------------------------------

def run_ffmpeg_with_progress(args: list[str], total_seconds: float, job: Job | None, cwd: Path | None = None,
                             what: str = "FFmpeg", log_path: Path | None = None) -> None:
    """Ejecuta FFmpeg leyendo ``-progress pipe:1`` para un porcentaje real."""
    full = args[:1] + ["-progress", "pipe:1", "-nostats"] + args[1:]
    log.debug("%s: %s", what, full)
    stderr_file = open(log_path, "w", encoding="utf-8", errors="replace") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=stderr_file if log_path else subprocess.PIPE,
                                stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd else None, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    except FileNotFoundError as exc:
        if log_path:
            stderr_file.close()
        raise RenderError("No se encuentra el ejecutable de FFmpeg.") from exc
    err_tail = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if job is not None and job.cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise Cancelled()
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    if job is not None and total_seconds > 0:
                        job.progress(us / 1e6 / total_seconds, f"{what}: {us / 1e6:.1f}/{total_seconds:.1f} s")
                except ValueError:
                    pass
        proc.wait()
        if not log_path and proc.stderr is not None:
            err_tail = proc.stderr.read()[-2000:]
    finally:
        if proc.poll() is None:
            proc.kill()
        if log_path:
            stderr_file.close()
    if proc.returncode != 0:
        if log_path and log_path.exists():
            err_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RenderError(f"{what} falló (código {proc.returncode}).\n{err_tail.strip()}")


# ---------------------------------------------------------------------------
# Clips por diapositiva
# ---------------------------------------------------------------------------

def _file_sig(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"


DESIGN_KEYS_FOR_CLIP = ("bg_color", "accent_color", "text_color", "title_size", "font_weight_bold", "composition",
                        "blur_darken", "motion_intensity", "image_shadow", "image_radius", "headline_style",
                        "font_file", "brand_text", "show_headlines", "subtitle_position", "subtitle_size",
                        "subtitle_max_lines", "subtitle_margin_x", "seed", "motion")


def clip_cache_key(source_sig: str, design: dict[str, Any], overrides: dict[str, Any], headline: str,
                   motion: str, frames: int, spec: OutputSpec) -> str:
    payload = json.dumps({
        "src": source_sig, "design": {k: design.get(k) for k in DESIGN_KEYS_FOR_CLIP},
        "ov": overrides, "head": headline, "motion": motion, "frames": frames,
        "size": [spec.width, spec.height], "fps": spec.fps, "v": RENDER_VERSION,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def build_static_clip_command(ffmpeg: str, frame_png: Path, frames: int, fps: int, out: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-i", str(frame_png),
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-f", "mp4", str(out)]


def build_pipe_clip_command(ffmpeg: str, width: int, height: int, fps: int, frames: int, out: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-f", "mp4", str(out)]


def render_clip(ffmpeg: str, comp: Composition, motion: str, intensity: float, frames: int, spec: OutputSpec,
                out: Path, job: Job | None = None, on_frames: Callable[[int], None] | None = None) -> None:
    """Renderiza un clip. Sin movimiento → imagen fija en bucle; con movimiento → fotogramas por tubería."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part.mp4")
    if motion == "none" or intensity <= 0:
        png = out.with_suffix(".frame.png")
        render_frame(Composition(comp.background, comp.foreground,
                                 (round(comp.fg_pos[0]), round(comp.fg_pos[1])), comp.mode), 1.0, 0, 0).save(png)
        proc = subprocess.run(build_static_clip_command(ffmpeg, png, frames, spec.fps, part), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        png.unlink(missing_ok=True)
        if proc.returncode != 0:
            part.unlink(missing_ok=True)
            raise RenderError(f"FFmpeg no pudo crear el clip estático:\n{proc.stderr[-1500:]}")
        if on_frames:
            on_frames(frames)
        part.replace(out)
        return

    args = build_pipe_clip_command(ffmpeg, spec.width, spec.height, spec.fps, frames, part)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    err_holder: list[bytes] = []
    t_err = threading.Thread(target=lambda: err_holder.append(proc.stderr.read() if proc.stderr else b""),
                             daemon=True)
    t_err.start()

    def make(i: int) -> bytes:
        p = i / max(1, frames - 1)
        z, dx, dy = motion_params(motion, p, intensity, spec.width, spec.height)
        return render_frame(comp, z, dx, dy).tobytes()

    workers = max(2, min(8, (os.cpu_count() or 4) - 1))
    try:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            batch = workers * 2
            for start in range(0, frames, batch):
                if job is not None and job.cancel_event.is_set():
                    raise Cancelled()
                idx = range(start, min(frames, start + batch))
                for data in pool.map(make, idx):
                    assert proc.stdin is not None
                    proc.stdin.write(data)
                if on_frames:
                    on_frames(len(idx))
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait()
    except (Cancelled, BrokenPipeError, OSError) as exc:
        proc.kill()
        proc.wait()
        part.unlink(missing_ok=True)
        if isinstance(exc, Cancelled):
            raise
        t_err.join(timeout=2)
        raise RenderError(f"FFmpeg cerró la tubería de vídeo: {(err_holder[0] if err_holder else b'').decode('utf-8', 'replace')[-1500:]}") from exc
    t_err.join(timeout=5)
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        raise RenderError(f"FFmpeg no pudo crear el clip animado:\n{(err_holder[0] if err_holder else b'').decode('utf-8', 'replace')[-1500:]}")
    part.replace(out)


# ---------------------------------------------------------------------------
# Montaje final
# ---------------------------------------------------------------------------

def build_final_command(ffmpeg: str, clips: list[Path], timeline: Timeline, audio_wav: Path, out: Path,
                        spec: OutputSpec, ass_name: str | None, fonts_dir: str | None,
                        video_fade: float = 0.0) -> list[str]:
    """Cadena xfade/concat + fundidos + subtítulos ASS → H.264/AAC."""
    if len(clips) != len(timeline.items):
        raise RenderError("Número de clips distinto del número de diapositivas.")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for c in clips:
        args += ["-i", str(c)]
    args += ["-i", str(audio_wav)]
    n = len(clips)
    parts = []
    for i in range(n):
        parts.append(f"[{i}:v]settb=AVTB,fps={spec.fps},format=yuv420p,setsar=1[v{i}]")
    cur = "[v0]"
    for i in range(1, n):
        it = timeline.items[i]
        nxt = f"[x{i}]"
        if it.transition_in > 0 and timeline.transition != "none":
            parts.append(f"{cur}[v{i}]xfade=transition={timeline.transition}:duration={it.transition_in:.4f}:"
                         f"offset={it.start:.4f}{nxt}")
        else:
            parts.append(f"{cur}[v{i}]concat=n=2:v=1:a=0{nxt}")
        cur = nxt
    post = []
    total = timeline.total
    if video_fade > 0:
        vf = min(video_fade, total / 4)
        post.append(f"fade=t=in:st=0:d={vf:.3f}")
        post.append(f"fade=t=out:st={max(0.0, total - vf):.3f}:d={vf:.3f}")
    if ass_name:
        post.append(f"ass={ass_name}" + (f":fontsdir={fonts_dir}" if fonts_dir else ""))
    post.append("format=yuv420p")
    parts.append(f"{cur}{','.join(post)}[vout]")
    args += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", f"{n}:a",
             "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf), "-pix_fmt", "yuv420p",
             "-profile:v", "high", "-r", str(spec.fps), "-g", str(spec.fps * 2),
             "-c:a", "aac", "-b:a", spec.audio_bitrate, "-ar", "48000",
             "-t", f"{total:.3f}", "-movflags", "+faststart", "-f", "mp4", str(out)]
    return args


# ---------------------------------------------------------------------------
# Narración
# ---------------------------------------------------------------------------

def narration_items(project: Project) -> list[dict[str, Any]]:
    """Elementos de la línea de tiempo (diapositivas activas + cierre opcional)."""
    items = []
    for s in project.enabled_slides():
        text = "" if s.get("role") == "silent" else normalize_text(s.get("narration", ""))
        items.append({"kind": "slide", "slide_id": s["id"], "image": s["image"], "text": text,
                      "headline": s.get("headline", ""), "overrides": s.get("overrides") or {}})
    outro = project.data.get("outro", {})
    o_text = normalize_text(outro.get("narration", ""))
    if outro.get("enabled") and o_text:
        if project.data["design"].get("outro_mode", "card") == "last_slide" and items:
            last = items[-1]
            items.append({"kind": "outro", "slide_id": "outro", "image": last["image"], "text": o_text,
                          "headline": last["headline"], "overrides": last["overrides"]})
        else:
            items.append({"kind": "outro", "slide_id": "outro", "image": "", "text": o_text,
                          "headline": "", "overrides": {}})
    return items


def synthesize_narration(project: Project, job: Job | None = None, engine: TTSEngine | None = None,
                         cache: NarrationCache | None = None) -> list[dict[str, Any]]:
    """Genera (o reutiliza) el audio de cada fragmento. Devuelve los elementos con su audio."""
    engine = engine or get_engine(project.data["voice"].get("engine", "piper"))
    lexicon = load_lexicon()
    cache = cache or NarrationCache()
    params = VoiceParams.from_project(project.data["voice"])
    items = narration_items(project)
    to_do = [it for it in items if it["text"]]
    if to_do:
        engine.check_voice(params.voice_id)
    done = 0
    reused = 0
    for it in items:
        if job is not None:
            job.check_cancel()
        if not it["text"]:
            it.update(audio_path=None, audio_duration=0.0, segments=[])
            continue
        res = cache.synthesize(engine, it["text"], params, lexicon)
        reused += int(res.cached)
        it.update(audio_path=res.wav_path, audio_duration=res.duration,
                  segments=[{"text": s.text, "start": s.start, "end": s.end} for s in res.segments])
        done += 1
        if job is not None:
            job.progress(done / len(to_do), f"Fragmento {done}/{len(to_do)} ({res.duration:.2f} s"
                         + (", caché" if res.cached else "") + ")")
    if job is not None and to_do:
        job.log(f"Narración: {len(to_do)} fragmentos ({reused} reutilizados de la caché).")
    # Guardar resumen de fragmentos en el proyecto (para la vista previa de fragmentos)
    project.data["narration_cache"] = [
        {"slide_id": it["slide_id"], "audio_path": it.get("audio_path"), "duration": it.get("audio_duration", 0.0),
         "segments": it.get("segments", []), "text": it["text"],
         "voice": f"{project.data['voice'].get('engine', 'piper')}|{project.data['voice'].get('voice_id', '')}"}
        for it in items]
    return items


def make_timeline(project: Project, items: list[dict[str, Any]], fps: int) -> Timeline:
    d = project.data["design"]
    t = project.data["timing"]
    for i, it in enumerate(items):
        it["motion"] = resolve_motion(d, it.get("overrides"), i) if it.get("image") else "none"
    tl = build_timeline(
        items, fps=fps, transition=d.get("transition", "fade"),
        transition_duration=float(d.get("transition_duration", 0.4)),
        min_slide_duration=float(d.get("min_slide_duration", 2.0)),
        cover_duration=float(d.get("cover_duration", 2.5)), lead_margin=float(t.get("lead_margin", 0.15)),
        pause_between=float(t.get("pause_between", 0.45)), silent_duration=float(t.get("silent_slide_duration", 2.5)),
        target_duration=float(t.get("target_duration", 0) or 0), fill_to_target=bool(t.get("fill_to_target")),
        video_fade=float(d.get("video_fade", 0.0)))
    errs = validate_timeline(tl)
    if errs:
        raise RenderError("Línea de tiempo incoherente: " + "; ".join(errs))
    return tl


# ---------------------------------------------------------------------------
# Composición de diapositivas
# ---------------------------------------------------------------------------

def compose_item(project: Project, item: dict[str, Any], spec: OutputSpec) -> tuple[Composition, str]:
    design = project.data["design"]
    if item["image"] == "" and item["kind"] == "outro":
        text = project.data["outro"].get("text") or project.data["outro"].get("narration", "")
        card = make_outro_card(design, spec.width, spec.height, text)
        sig = hashlib.sha256(f"outro:{text}".encode("utf-8")).hexdigest()[:16]
        empty = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        return Composition(card, empty, (0, 0), "solid"), sig
    path = project.image_path(item["image"])
    img = load_image(path)
    comp = compose_slide(img, design, spec.width, spec.height, item.get("overrides"),
                         item.get("headline", "") if item["kind"] == "slide" else "")
    return comp, _file_sig(path)


def render_still(project: Project, slide_index: int, sample_text: str | None, out_png: Path,
                 preview: bool = True, settings: Settings | None = None) -> tuple[Path, list[str]]:
    """Fotograma de una diapositiva con subtítulo de ejemplo renderizado por libass (estilo real)."""
    s = settings or get_settings()
    spec = OutputSpec.from_project(project, preview)
    slides = project.data["slides"]
    if not slides:
        raise RenderError("El proyecto no tiene imágenes.")
    slide_index = max(0, min(len(slides) - 1, slide_index))
    sl = slides[slide_index]
    item = {"kind": "slide", "image": sl["image"], "headline": sl.get("headline", ""),
            "overrides": sl.get("overrides") or {}}
    comp, _ = compose_item(project, item, spec)
    frame = render_frame(comp, 1.0, 0, 0)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if not sample_text:
        frame.save(out_png)
        return out_png, comp.warnings
    ff = inspect_ffmpeg(s)
    if not ff.ffmpeg or not ff.has_ass:
        frame.save(out_png)
        return out_png, comp.warnings + ["FFmpeg con libass no disponible: se muestra sin subtítulo."]
    work = s.temp_path / f"still-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        frame.save(work / "frame.png")
        font_path = resolve_font_path(project.data["design"].get("font_file", ""), True)
        fonts_dir = _stage_fonts(work, font_path)
        write_ass(sample_cues(sample_text, project.data["design"]), work / "subs.ass", project.data["design"],
                  spec.width, spec.height, font_family_name(font_path))
        vf = "ass=subs.ass" + (f":fontsdir={fonts_dir}" if fonts_dir else "")
        proc = subprocess.run([ff.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", "frame.png",
                               "-vf", vf, "-frames:v", "1", "out.png"], cwd=str(work), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise RenderError(f"No se pudo previsualizar el subtítulo: {proc.stderr[-800:]}")
        shutil.copyfile(work / "out.png", out_png)
    finally:
        safe_rmtree(work, s.temp_path)
    return out_png, comp.warnings


def _stage_fonts(work: Path, font_path: Path | None) -> str | None:
    if font_path is None:
        return None
    fd = work / "fonts"
    fd.mkdir(exist_ok=True)
    shutil.copyfile(font_path, fd / re.sub(r"[^A-Za-z0-9_.-]", "_", font_path.name))
    return "fonts"


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    """Borra ``path`` sólo si está dentro de ``allowed_root`` (nunca fuera)."""
    try:
        p = Path(path).resolve()
        root = Path(allowed_root).resolve()
        p.relative_to(root)
        if p == root:
            return
    except (ValueError, OSError):
        log.error("Se ha evitado borrar %s: está fuera de la carpeta temporal.", path)
        return
    shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------

def output_paths(project: Project, spec: OutputSpec, settings: Settings) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = slugify(project.data.get("title") or project.name)
    suffix = "_preview" if spec.preview else ""
    d = settings.outputs_path / project.id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{base}_{stamp}{suffix}.mp4", d / f"{base}_{stamp}{suffix}.srt"


def music_path_for(project: Project, settings: Settings) -> Path | None:
    mf = (project.data["audio"].get("music_file") or "").strip()
    if not mf:
        return None
    p = Path(mf)
    return p if p.is_absolute() else settings.music_path / mf


def render_project(project: Project, job: Job, preview: bool, settings: Settings | None = None,
                   engine: TTSEngine | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    if not _render_lock.acquire(blocking=False):
        raise RenderError("Ya hay un renderizado en curso.")
    work: Path | None = None
    try:
        spec = OutputSpec.from_project(project, preview)
        job.set_phase("Validaciones previas")
        ff = inspect_ffmpeg(s)
        eng = engine or get_engine(project.data["voice"].get("engine", "piper"))
        try:
            needs_voice = any(it["text"] for it in narration_items(project))
            if needs_voice:
                eng.check_voice(project.data["voice"].get("voice_id", ""))
            voice_ok = (True, "")
        except TTSError as exc:
            voice_ok = (False, str(exc))
        font_path = resolve_font_path(project.data["design"].get("font_file", ""), True)
        music = music_path_for(project, s)
        rep = preflight(project, ff, preview=preview, voice_ok=voice_ok, music_path=music, font_path=font_path,
                        settings=s)
        for w in rep.warnings:
            job.log(f"Aviso: {w}")
        if not rep.ok:
            raise RenderError("No se puede renderizar:\n- " + "\n- ".join(rep.errors))
        job.check_cancel()

        job.set_phase("Narración", "sintetizando o reutilizando la caché")
        items = synthesize_narration(project, job, eng)
        project.save()

        job.set_phase("Línea de tiempo")
        tl = make_timeline(project, items, spec.fps)
        for w in tl.warnings:
            job.log(f"Aviso: {w}")
        if tl.total > 180:
            raise RenderError(f"La duración calculada ({tl.total:.0f} s) supera 3 minutos.")
        job.log(f"Duración total {tl.total:.2f} s · narración {tl.narration_total:.2f} s · {len(tl.items)} tramos")

        run_id = f"{project.id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        work = s.temp_path / run_id
        work.mkdir(parents=True, exist_ok=True)

        job.set_phase("Subtítulos")
        d = project.data["design"]
        cues = build_cues(tl, int(d.get("subtitle_max_chars", 26)), int(d.get("subtitle_max_lines", 2)))
        fonts_dir = _stage_fonts(work, font_path)
        write_ass(cues, work / "subs.ass", d, spec.width, spec.height, font_family_name(font_path))
        mp4_out, srt_out = output_paths(project, spec, s)
        write_srt(cues, srt_out)
        shutil.copyfile(work / "subs.ass", project.subtitles_dir / ("preview.ass" if preview else "final.ass"))
        shutil.copyfile(srt_out, project.subtitles_dir / ("preview.srt" if preview else "final.srt"))
        job.log(f"{len(cues)} bloques de subtítulos.")

        job.set_phase("Clips de diapositivas")
        clip_dir = s.cache_path / "clips"
        clip_dir.mkdir(parents=True, exist_ok=True)
        total_frames = sum(int(round(it.clip_duration * spec.fps)) for it in tl.items)
        done_frames = [0]

        def on_frames(k: int) -> None:
            done_frames[0] += k
            job.progress(done_frames[0] / max(1, total_frames),
                         f"{done_frames[0]}/{total_frames} fotogramas")

        clips: list[Path] = []
        reused = 0
        intensity = float(d.get("motion_intensity", 0.5))
        for it_tl, it in zip(tl.items, items):
            job.check_cancel()
            frames = int(round(it_tl.clip_duration * spec.fps))
            comp, sig = compose_item(project, it, spec)
            for w in comp.warnings:
                job.log(f"Aviso ({it['image'] or 'cierre'}): {w}")
            motion = it_tl.motion if it["image"] else "none"
            key = clip_cache_key(sig, d, it.get("overrides") or {}, it.get("headline", ""), motion, frames, spec)
            clip = clip_dir / f"{key}.mp4"
            meta = clip_dir / f"{key}.json"
            if clip.is_file() and meta.is_file() and clip.stat().st_size > 0:
                reused += 1
                on_frames(frames)
            else:
                render_clip(ff.ffmpeg, comp, motion, intensity, frames, spec, clip, job, on_frames)
                meta.write_text(json.dumps({"frames": frames, "fps": spec.fps, "created": time.time()}),
                                encoding="utf-8")
            clips.append(clip)
        job.log(f"Clips: {len(clips)} ({reused} reutilizados de la caché).")

        job.set_phase("Mezcla de audio")
        mix_wav = work / "mix.wav"
        stats = audio_mixer.mix(ff.ffmpeg, tl, project.data["audio"], mix_wav, music)
        job.log(f"Mezcla: volumen medio {stats.mean_db:.1f} dB, pico {stats.peak_db:.1f} dB.")

        job.set_phase("Montaje y codificación final")
        part = mp4_out.with_name(mp4_out.stem + ".part.mp4")
        args = build_final_command(ff.ffmpeg, clips, tl, mix_wav, part, spec, "subs.ass", fonts_dir,
                                   float(d.get("video_fade", 0.0)))
        run_ffmpeg_with_progress(args, tl.total, job, cwd=work, what="Codificación",
                                 log_path=work / "ffmpeg_final.log")
        part.replace(mp4_out)

        job.set_phase("Verificación con ffprobe")
        narr_end = max((it.narration_end or 0.0) for it in tl.items)
        post = postflight(mp4_out, ff, width=spec.width, height=spec.height, expected_duration=tl.total,
                          narration_end=narr_end, srt_path=srt_out)
        for e in post.errors:
            job.log(f"✖ {e}")
        report = {
            "output": str(mp4_out), "srt": str(srt_out), "preview": preview, "duration": tl.total,
            "narration_total": tl.narration_total, "postflight": post.to_dict(), "preflight": rep.to_dict(),
            "timeline_warnings": tl.warnings, "clips_reused": reused, "clips_total": len(clips),
            "timeline": [{"i": it.index, "image": it.image or "(cierre)", "start": round(it.start, 3),
                          "slot": round(it.slot, 3), "narration_start": it.narration_start,
                          "narration_end": it.narration_end, "motion": it.motion} for it in tl.items],
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        if not preview and post.ok and (project.data.get("thumbnail") or {}).get("auto", True):
            from thumbnail import ThumbnailError, render_project_thumbnail
            try:
                thumb = render_project_thumbnail(project, mp4_out.with_name(mp4_out.stem + "_miniatura.jpg"))
                report["thumbnail"] = str(thumb)
                job.log(f"Miniatura: {thumb.name}")
            except (ThumbnailError, OSError) as exc:
                job.log(f"⚠️ No se pudo crear la miniatura: {exc}")
        mp4_out.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
        project.data["last_export"] = {"preview" if preview else "final": report}
        project.save()
        if not post.ok:
            raise RenderError("El vídeo se generó pero no supera la verificación:\n- " + "\n- ".join(post.errors))
        return report
    except BaseException:
        # Conservar el registro de FFmpeg para diagnóstico y no dejar archivos a medias
        if work is not None:
            logf = work / "ffmpeg_final.log"
            if logf.exists():
                try:
                    s.logs_path.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(logf, s.logs_path / f"ffmpeg_{work.name}.log")
                except OSError:
                    pass
        for leftover in s.outputs_path.glob(f"{project.id}/*.part.mp4"):
            leftover.unlink(missing_ok=True)
        raise
    finally:
        if work is not None:
            safe_rmtree(work, s.temp_path)
        _render_lock.release()


def preview_mix(project: Project, job: Job, settings: Settings | None = None,
                engine: TTSEngine | None = None) -> dict[str, Any]:
    """Genera sólo la mezcla de audio (voz + música) para escucharla antes de renderizar."""
    s = settings or get_settings()
    ff = inspect_ffmpeg(s)
    if not ff.ffmpeg:
        raise RenderError("No se encuentra FFmpeg.")
    job.set_phase("Narración")
    items = synthesize_narration(project, job, engine)
    project.save()
    job.set_phase("Mezcla de audio")
    tl = make_timeline(project, items, int(project.data["export"].get("fps", 30)))
    out = project.audio_dir / "mezcla_prueba.wav"
    stats = audio_mixer.mix(ff.ffmpeg, tl, project.data["audio"], out, music_path_for(project, s))
    return {"wav": str(out), "duration": tl.total, "mean_db": stats.mean_db, "peak_db": stats.peak_db}


def clear_cache(settings: Settings | None = None) -> float:
    s = settings or get_settings()
    freed = 0
    for sub in ("clips", "tts"):
        d = s.cache_path / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink(missing_ok=True)
    return freed / 1e6
