"""Prueba de extremo a extremo con imágenes sintéticas y el motor de voz de prueba."""
import json
from pathlib import Path

import pytest

from conftest import ToneTTS, make_image, needs_ffmpeg
from image_processor import scan_folder
from jobs import Job
from project_manager import ProjectManager
from quality_checks import inspect_ffmpeg, postflight
from video_renderer import RenderError, render_project, render_still


def prepare(settings, image_folder):
    pm = ProjectManager()
    p = pm.create("Prueba E2E")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    texts = ["¿Buscas herramientas útiles? Aquí tienes cinco.",
             "La primera automatiza tareas repetitivas.",
             "La segunda organiza tus notas, sin complicaciones.",
             "La tercera te ayuda a revisar código.",
             "Y la última genera documentación."]
    for s, t in zip(p.data["slides"], texts):
        s["narration"] = t
    p.data["slides"][1]["headline"] = "Herramienta nº 1"
    p.data["outro"].update(enabled=True, narration="Guarda este vídeo y sígueme.", text="¡Sígueme!")
    p.data["voice"]["voice_id"] = "tono"
    p.data["design"]["motion_intensity"] = 0.6
    p.data["design"]["brand_text"] = "@canal"
    p.save()
    return pm, p


@needs_ffmpeg
def test_full_preview_and_cache(settings, image_folder, tmp_path):
    pm, p = prepare(settings, image_folder)
    music = make_music(settings)
    p.data["audio"]["music_file"] = music.name
    eng = ToneTTS()
    job = Job("render", p.id)
    rep = render_project(p, job, preview=True, settings=settings, engine=eng)
    out = Path(rep["output"])
    assert out.is_file() and Path(rep["srt"]).is_file()
    assert rep["postflight"]["errors"] == []
    assert rep["clips_reused"] == 0
    assert not list(settings.temp_path.iterdir())             # temporales limpiados
    calls = eng.calls

    # Reabrir el proyecto y renderizar de nuevo: caché de voz y de clips
    q = pm.open(p.id)
    assert q.data["audio"]["music_file"] == music.name
    job2 = Job("render", q.id)
    rep2 = render_project(q, job2, preview=True, settings=settings, engine=eng)
    assert eng.calls == calls                                  # sin nueva síntesis
    assert rep2["clips_reused"] == rep2["clips_total"]
    assert abs(rep2["duration"] - rep["duration"]) < 1e-6

    # Cambiar un texto sólo regenera ese fragmento
    q.data["slides"][2]["narration"] = "La segunda organiza tus notas de forma sencilla."
    rep3 = render_project(q, Job("render", q.id), preview=True, settings=settings, engine=eng)
    assert eng.calls > calls
    assert rep3["postflight"]["errors"] == []


def make_music(settings):
    import numpy as np
    from tts_engine import write_wav
    t = np.arange(22050 * 3) / 22050
    p = settings.music_path / "musica_libre.wav"
    write_wav(p, (0.4 * np.sin(2 * np.pi * 196 * t)).astype("float32"), 22050)
    return p


@needs_ffmpeg
def test_final_resolution_is_1080x1920(settings, image_folder):
    _, p = prepare(settings, image_folder)
    p.data["slides"] = p.data["slides"][:2]
    p.data["outro"]["enabled"] = False
    p.data["export"]["preset"] = "ultrafast"
    p.data["design"]["motion"] = "none"
    rep = render_project(p, Job("render", p.id), preview=False, settings=settings, engine=ToneTTS())
    assert "Dimensiones correctas: 1080×1920 (9:16)." in rep["postflight"]["info"]
    thumb = Path(rep["thumbnail"])                       # miniatura creada junto al vídeo final
    assert thumb.is_file() and thumb.name.endswith("_miniatura.jpg") and thumb.parent == Path(rep["output"]).parent


@needs_ffmpeg
def test_preflight_blocks_missing_narration_and_voice(settings, image_folder):
    _, p = prepare(settings, image_folder)
    p.data["slides"][0]["narration"] = ""
    with pytest.raises(RenderError, match="falta la narración"):
        render_project(p, Job("render", p.id), preview=True, settings=settings, engine=ToneTTS())
    p.data["slides"][0]["narration"] = "Hola."
    p.data["voice"]["voice_id"] = "voz-inexistente"
    with pytest.raises(RenderError, match="no está instalada"):
        render_project(p, Job("render", p.id), preview=True, settings=settings, engine=ToneTTS())


@needs_ffmpeg
def test_cancel(settings, image_folder):
    _, p = prepare(settings, image_folder)
    job = Job("render", p.id)
    job.cancel_event.set()
    from jobs import Cancelled
    with pytest.raises(Cancelled):
        render_project(p, job, preview=True, settings=settings, engine=ToneTTS())
    assert not list(settings.outputs_path.rglob("*.part.mp4"))


@needs_ffmpeg
def test_postflight_detects_bad_files(settings, tmp_path):
    ff = inspect_ffmpeg(settings)
    bad = tmp_path / "roto.mp4"
    bad.write_bytes(b"\x00" * 1000)
    rep = postflight(bad, ff, width=1080, height=1920, expected_duration=10, narration_end=9, srt_path=None)
    assert not rep.ok
    rep = postflight(tmp_path / "no.mp4", ff, width=1080, height=1920, expected_duration=10, narration_end=9,
                     srt_path=None)
    assert "no existe" in rep.errors[0]


@needs_ffmpeg
def test_style_preview_still(settings, image_folder, tmp_path):
    _, p = prepare(settings, image_folder)
    out, warnings = render_still(p, 0, "¿Así se ven los subtítulos? ¡Sí, con acentos!", tmp_path / "s.png",
                                 settings=settings)
    from PIL import Image
    assert Image.open(out).size == (540, 960)
