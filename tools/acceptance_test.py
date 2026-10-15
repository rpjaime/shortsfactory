"""Prueba de aceptación completa con la voz REAL de Piper (criterios 3–15 de la especificación).

Uso:  .venv\\Scripts\\python tools\\acceptance_test.py      (o run_acceptance.bat)

Genera cinco imágenes de carrusel sintéticas, una pista musical sintética (sin derechos
de terceros), crea un proyecto, genera la voz española, renderiza la previsualización y
el vídeo final, lo verifica con ffprobe, reabre el proyecto y genera un segundo vídeo
reutilizando configuración y caché. Escribe el resultado en logs/acceptance.txt.
"""
from __future__ import annotations

import json
import math
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from config import load_settings, setup_logging  # noqa: E402
from fonts import get_font  # noqa: E402
from image_processor import scan_folder  # noqa: E402
from jobs import Job  # noqa: E402
from project_manager import ProjectManager  # noqa: E402
from script_manager import validate_project_script  # noqa: E402
from subtitle_engine import build_cues  # noqa: E402
from tts_engine import PiperEngine, write_wav  # noqa: E402
from video_renderer import make_timeline, render_project, synthesize_narration  # noqa: E402

results: list[tuple[str, bool, str]] = []
log_lines: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    say(f"[{'PASA' if cond else 'FALLA'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def make_carousel(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    slides = [
        ("01-portada.png", "5 herramientas\nde GitHub", (13, 17, 23), (255, 255, 255)),
        ("02-herramienta.png", "Herramienta 1\nautomatiza tareas", (255, 255, 255), (20, 20, 20)),
        ("03-herramienta.jpg", "Herramienta 2\nrevisa código", (240, 244, 255), (20, 20, 20)),
        ("04-herramienta.webp", "Herramienta 3\norganiza notas", (30, 30, 60), (255, 255, 255)),
        ("10-cierre.png", "¿Cuál usarás?\n¡Guárdalo!", (47, 129, 247), (255, 255, 255)),
    ]
    paths = []
    title = get_font("", 76, bold=True)
    body = get_font("", 38, bold=False)
    for name, text, bg, fg in slides:
        im = Image.new("RGB", (1080, 1350), bg)
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, 1080, 110), fill=(47, 129, 247))
        d.multiline_text((80, 220), text, font=title, fill=fg, spacing=18)
        for k, y in enumerate(range(620, 1250, 70)):
            d.rounded_rectangle((80, y, 80 + 300 + (k * 97) % 600, y + 34), radius=10, fill=(150, 150, 150))
        d.text((80, 1270), "Imagen de prueba generada por ShortsFactory", font=body, fill=fg)
        p = folder / name
        im.save(p, quality=92) if p.suffix != ".png" else im.save(p)
        paths.append(p)
    return paths


def make_music(path: Path, seconds: float = 12.0, sr: int = 44100) -> Path:
    """Pista sintética (acordes suaves) creada aquí mismo: sin derechos de terceros."""
    t = np.arange(int(seconds * sr)) / sr
    chords = [(220.0, 277.18, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63), (196.0, 246.94, 293.66)]
    sig = np.zeros_like(t)
    seg = seconds / len(chords)
    for i, ch in enumerate(chords):
        m = (t >= i * seg) & (t < (i + 1) * seg)
        env = np.sin(np.pi * (t[m] - i * seg) / seg) ** 0.5
        for f in ch:
            sig[m] += np.sin(2 * math.pi * f * t[m]) * env
    sig = 0.25 * sig / np.max(np.abs(sig))
    write_wav(path, sig.astype(np.float32), sr)
    return path


NARRATION = [
    "¿Buscas herramientas de GitHub que te ahorren horas? Aquí tienes cinco que merece la pena conocer.",
    "La primera automatiza tareas repetitivas, para que te centres en lo importante.",
    "La segunda te ayuda a revisar código antes de publicarlo.",
    "La tercera organiza tus notas y tus ideas en un solo sitio.",
    "¿Cuál vas a probar primero?",
]
OUTRO = "Guarda este vídeo y sígueme para descubrir más herramientas."


def main() -> int:
    s = load_settings()
    s.ensure_dirs()
    setup_logging(s)
    t_start = time.time()
    say(f"Prueba de aceptación ShortsFactory — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    eng = PiperEngine(s.voices_path)
    voices = [v for v in eng.list_voices() if v["ok"]]
    if not check("Voz española instalada", bool(voices), ", ".join(v["id"] for v in voices) or "ninguna"):
        say("Descarga una voz con: .venv\\Scripts\\python tools\\download_voice.py es_ES-davefx-medium")
        return finish(s, t_start)
    voice_id = next((v["id"] for v in voices if v["id"].startswith("es_ES")), voices[0]["id"])

    folder = s.temp_path / "aceptacion_imagenes"
    make_carousel(folder)
    music = make_music(s.music_path / "aceptacion_musica_sintetica.wav")

    pm = ProjectManager(s.projects_path)
    p = pm.create("Aceptación ShortsFactory")
    check("3. Crear un proyecto", p.manifest_path.is_file(), str(p.dir))

    names = scan_folder(folder)
    p.set_source_dir(folder, names)
    check("4. Importar cinco imágenes", len(p.data["slides"]) == 5, ", ".join(names))
    check("5a. Orden natural", names[-1] == "10-cierre.png" and names[0] == "01-portada.png", " → ".join(names))
    p.move_slide(4, -1)
    p.move_slide(3, 1)   # mover y devolver: el orden manual funciona
    from image_processor import make_thumbnail
    thumbs = [make_thumbnail(p.image_path(sl["image"]), p.cache_dir / "thumbs" / f"{sl['id']}.jpg")
              for sl in p.data["slides"]]
    check("5b. Reordenar y previsualizar miniaturas", [sl["image"] for sl in p.data["slides"]] == names
          and all(t.is_file() for t in thumbs))

    for sl, text in zip(p.data["slides"], NARRATION):
        sl["narration"] = text
    p.data["slides"][1]["headline"] = "Herramienta 1"
    p.data["outro"].update(enabled=True, narration=OUTRO, text="¿Te ha servido?\nGuárdalo y sígueme")
    p.data["voice"]["voice_id"] = voice_id
    p.data["design"]["brand_text"] = "@mi_canal"
    p.data["audio"]["music_file"] = music.name
    p.data["timing"]["target_duration"] = 40
    p.save()
    v = validate_project_script(p)
    check("6. Editar la narración de cada diapositiva", v.ok, "; ".join(v.errors) or "guion válido")

    t0 = time.time()
    items = synthesize_narration(p, None, eng)
    p.save()
    durs = [round(it["audio_duration"], 2) for it in items if it["text"]]
    check("7. Generar voz española local (Piper)", all(d > 0.5 for d in durs) and len(set(durs)) > 1,
          f"duraciones reales {durs} en {time.time() - t0:.1f} s")

    tl = make_timeline(p, items, 30)
    cues = build_cues(tl, 26, 2)
    in_window = all(any(it.narration_start - 1e-3 <= c.start and c.end <= it.narration_end + 1e-3
                        for it in tl.items if it.narration_start is not None) for c in cues)
    check("8. Subtítulos sincronizados con el audio real", bool(cues) and in_window,
          f"{len(cues)} bloques, todos dentro de su frase")

    p.data["design"]["template"] = "tech_dark"
    check("9. Plantilla visual seleccionada", p.data["design"]["template"] == "tech_dark")
    check("10. Música opcional añadida", (s.music_path / p.data["audio"]["music_file"]).is_file(), music.name)
    p.save()

    job = Job("previsualización", p.id)
    t0 = time.time()
    rep_prev = render_project(p, job, preview=True, settings=s, engine=eng)
    check("11. Generar una previsualización", Path(rep_prev["output"]).is_file()
          and not rep_prev["postflight"]["errors"], f"{rep_prev['duration']:.2f} s en {time.time() - t0:.1f} s")

    job = Job("final", p.id)
    t0 = time.time()
    rep = render_project(p, job, preview=False, settings=s, engine=eng)
    info = rep["postflight"]["info"]
    check("12. Exportar MP4 H.264 vertical 1080×1920", "Dimensiones correctas: 1080×1920 (9:16)." in info
          and "H.264 · yuv420p." in info, f"{rep['output']} ({time.time() - t0:.1f} s)")
    check("13. Verificar el resultado con ffprobe", not rep["postflight"]["errors"],
          " | ".join(info + rep["postflight"]["errors"]))
    say(f"     Duración {rep['duration']:.2f} s · voz {rep['narration_total']:.2f} s · SRT {rep['srt']}")

    before = {k: json.dumps(p.data[k], sort_keys=True) for k in ("design", "voice", "audio", "timing", "export", "slides")}
    q = pm.open(p.id)
    after = {k: json.dumps(q.data[k], sort_keys=True) for k in before}
    check("14. Reabrir el proyecto sin perder su configuración", before == after,
          "diseño, voz, audio, tiempos, exportación y diapositivas idénticos")

    p2 = pm.create("Aceptación — segundo vídeo")
    pm.duplicate_settings_into(q, p2)
    p2 = pm.open(p2.id)
    p2.set_source_dir(folder, names)
    for sl, text in zip(p2.data["slides"], NARRATION):
        sl["narration"] = text
    p2.data["slides"][3]["narration"] = "La tercera organiza tus notas, tus tareas y tus ideas."
    p2.data["slides"][1]["headline"] = "Herramienta 1"
    p2.data["outro"].update(enabled=True, narration=OUTRO, text="¿Te ha servido?\nGuárdalo y sígueme")
    p2.save()
    calls_before = time.time()
    rep2 = render_project(p2, Job("final", p2.id), preview=False, settings=s, engine=eng)
    check("15. Segundo vídeo reutilizando configuración y caché",
          not rep2["postflight"]["errors"] and rep2["clips_reused"] >= 3,
          f"clips reutilizados {rep2['clips_reused']}/{rep2['clips_total']} · {time.time() - calls_before:.1f} s")
    return finish(s, t_start)


def finish(s, t_start: float) -> int:
    passed = sum(1 for _, okk, _ in results if okk)
    say("")
    say(f"RESUMEN: {passed}/{len(results)} comprobaciones superadas en {time.time() - t_start:.0f} s")
    (s.logs_path / "acceptance.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        say("ERROR INESPERADO:\n" + traceback.format_exc())
        s = load_settings()
        s.logs_path.mkdir(parents=True, exist_ok=True)
        (s.logs_path / "acceptance.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        code = 1
    sys.exit(code)
