"""Diagnóstico del entorno: Python, dependencias, FFmpeg, Piper, voces, fuentes y LM Studio.

Uso:  python check_environment.py      (lo ejecuta check_environment.bat)
Escribe el informe en logs/check_environment.txt. Código de salida 0 = listo para generar vídeos.
"""
from __future__ import annotations

import importlib
import os
import platform
import sys
import time
import traceback
from pathlib import Path

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import APP_NAME, APP_VERSION, load_settings  # noqa: E402

lines: list[str] = []
critical: list[str] = []


def out(msg: str = "") -> None:
    print(msg)
    lines.append(msg)


def ok(msg: str) -> None:
    out(f"  [OK]    {msg}")


def warn(msg: str) -> None:
    out(f"  [AVISO] {msg}")


def fail(msg: str) -> None:
    out(f"  [ERROR] {msg}")
    critical.append(msg)


def main() -> int:
    s = load_settings()
    s.ensure_dirs()
    out(f"{APP_NAME} {APP_VERSION} — diagnóstico del entorno ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    out(f"Carpeta de la aplicación: {Path(__file__).resolve().parent}")
    out()
    out("Python")
    v = sys.version_info
    (ok if (v.major, v.minor) == (3, 11) else warn)(
        f"Python {platform.python_version()} en {sys.executable}" +
        ("" if (v.major, v.minor) == (3, 11) else " (versión probada: 3.11)"))
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    (ok if in_venv else warn)("Entorno virtual activo" if in_venv else "No se está usando el entorno virtual .venv")
    out(f"  Sistema: {platform.platform()} · CPU: {platform.processor() or '?'} · núcleos: {os.cpu_count()}")
    out()
    out("Dependencias")
    for mod, attr in (("gradio", "__version__"), ("piper", None), ("kokoro_onnx", None), ("onnxruntime", "__version__"),
                      ("PIL", "__version__"), ("numpy", "__version__"), ("pandas", "__version__"),
                      ("pytest", "__version__")):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, attr, "") if attr else ""
            if mod in ("piper", "kokoro_onnx"):
                from importlib.metadata import version
                ver = version("piper-tts" if mod == "piper" else "kokoro-onnx")
            ok(f"{mod} {ver}")
        except Exception as exc:  # noqa: BLE001
            (warn if mod == "pytest" else fail)(f"{mod}: no se puede importar ({exc}). Ejecuta install.bat.")
    try:
        import onnxruntime as ort
        ok(f"onnxruntime proveedores: {', '.join(ort.get_available_providers())} (se usa CPU)")
    except Exception:  # noqa: BLE001
        pass
    out()
    out("FFmpeg")
    from quality_checks import inspect_ffmpeg
    info = inspect_ffmpeg(s)
    if info.ok:
        ok(f"FFmpeg {info.version}: {info.ffmpeg}")
        ok(f"ffprobe: {info.ffprobe}")
        ok("libx264, AAC, libass (ass), xfade y sidechaincompress disponibles")
    else:
        for p in info.problems():
            fail(p)
    out()
    out("Piper y voces")
    from tts_engine import NarrationCache, PiperEngine, TTSError, VoiceParams
    avail, msg = PiperEngine.is_available()
    if not avail:
        fail(msg)
    eng = PiperEngine(s.voices_path)
    voices = eng.list_voices()
    out(f"  Carpeta de voces: {s.voices_path}")
    if not voices:
        fail("No hay ninguna voz instalada. Ejecuta: .venv\\Scripts\\python tools\\download_voice.py es_ES-davefx-medium")
    for vo in voices:
        (ok if vo["ok"] else fail)(f"{vo['label']}" + ("" if vo["ok"] else f": {vo['error']}"))
    good = [vo for vo in voices if vo["ok"]]
    if avail and good:
        vid = good[0]["id"]
        try:
            import tempfile
            t0 = time.time()
            with tempfile.TemporaryDirectory() as td:
                res = eng.synthesize("¿Funciona la voz española? ¡Sí, perfectamente!", VoiceParams(vid),
                                     Path(td) / "prueba.wav")
            ok(f"Síntesis real con {vid}: {res.duration:.2f} s de audio en {time.time() - t0:.2f} s, "
               f"{len(res.segments)} frases con tiempos reales")
        except TTSError as exc:
            fail(f"La síntesis de prueba falló: {exc}")
    out()
    out("Kokoro (opcional, voces más naturales)")
    from tts_engine import KokoroEngine
    kok_ok, kmsg = KokoroEngine.is_available()
    keng = KokoroEngine(s.voices_path / "kokoro")
    if not kok_ok:
        warn(kmsg)
    elif not keng.models_installed():
        warn("Modelo Kokoro no descargado (pestaña Voz → Descargar voces → Kokoro-82M).")
    else:
        try:
            import tempfile
            t0 = time.time()
            with tempfile.TemporaryDirectory() as td:
                res = keng.synthesize("¿Funciona Kokoro en español? ¡Sí!", VoiceParams("ef_dora"), Path(td) / "k.wav")
            ok(f"Síntesis real con Kokoro (ef_dora): {res.duration:.2f} s de audio en {time.time() - t0:.2f} s")
        except TTSError as exc:
            fail(f"Kokoro falló: {exc}")
    out()
    out("Voz clonada (opcional, Chatterbox)")
    from tts_engine import ChatterboxEngine
    ceng = ChatterboxEngine()
    c_ok, cmsg = ceng.is_available()
    if not c_ok:
        warn(cmsg)
    else:
        refs = ceng.references()
        ok(f"Chatterbox instalado en .venv-voz (decodificador {'España v3' if (ceng.model_dir / 's3gen_v3.safetensors').is_file() else 'genérico'})")
        (ok if refs else warn)(f"Grabaciones de referencia: {', '.join(p.name for p in refs) or 'ninguna (pestaña Voz → Voz clonada)'}")
    out()
    out("Tipografía")
    from fonts import resolve_font_path, supports_spanish
    fp = resolve_font_path("", True)
    if fp is None:
        fail("No se encontró ninguna fuente TTF/OTF.")
    else:
        (ok if supports_spanish(fp) else warn)(f"Fuente predeterminada: {fp}")
    out()
    out("LM Studio (opcional)")
    from script_manager import LLMError, lmstudio_list_models
    try:
        models = lmstudio_list_models(s.lmstudio_url, timeout=3)
        ok(f"Conectado a {s.lmstudio_url}: {', '.join(models) or 'sin modelos cargados'}")
    except LLMError as exc:
        warn(f"No disponible ({exc.__class__.__name__}). La aplicación funciona sin LM Studio.")
    out()
    out("Espacio en disco")
    from quality_checks import free_space_gb
    for label, path in (("salida", s.outputs_path), ("caché", s.cache_path), ("temporales", s.temp_path)):
        gb = free_space_gb(path)
        (ok if gb >= 2 else fail)(f"{label}: {gb:.1f} GB libres ({path})")
    out()
    if critical:
        out(f"RESULTADO: {len(critical)} problema(s) que impiden generar vídeos. Revisa los [ERROR] de arriba.")
    else:
        out("RESULTADO: entorno listo para generar vídeos.")
    return 1 if critical else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:  # noqa: BLE001
        out("Fallo inesperado del diagnóstico:\n" + traceback.format_exc())
    finally:
        try:
            s = load_settings()
            s.logs_path.mkdir(parents=True, exist_ok=True)
            (s.logs_path / "check_environment.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    sys.exit(code)
