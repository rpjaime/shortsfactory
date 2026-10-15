"""Utilidades comunes de las pruebas.

Las pruebas usan imágenes y audios sintéticos pequeños y un motor de voz de prueba
(tonos) para no depender de descargar modelos. Las pruebas que necesitan FFmpeg se
omiten automáticamente si no está instalado.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402  (también desactiva la telemetría de onnxruntime)
from tts_engine import TTSEngine, VoiceParams  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg/ffprobe no están en el PATH")


@pytest.fixture()
def settings(tmp_path):
    s = config.Settings(
        projects_dir=str(tmp_path / "projects"), outputs_dir=str(tmp_path / "outputs"),
        temp_dir=str(tmp_path / "temp"), logs_dir=str(tmp_path / "logs"),
        assets_dir=str(tmp_path / "assets"), cache_dir=str(tmp_path / "cache"),
    )
    s.ensure_dirs()
    config.set_settings_for_tests(s)
    yield s
    config.set_settings_for_tests(config.Settings())


def make_image(path: Path, size=(1080, 1350), color=(40, 90, 160), text="", mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "RGBA":
        im = Image.new("RGBA", size, color + (0,))
        ImageDraw.Draw(im).ellipse((size[0] // 4, size[1] // 4, 3 * size[0] // 4, 3 * size[1] // 4),
                                   fill=color + (255,))
    else:
        im = Image.new("RGB", size, color)
        d = ImageDraw.Draw(im)
        d.rectangle((40, 40, size[0] - 40, 160), fill=(255, 255, 255))
        d.text((60, 80), text or path.stem, fill=(0, 0, 0))
        for i in range(0, size[0], 90):
            d.line((i, 200, size[0] - i, size[1]), fill=(255, 200, 0), width=3)
    im.save(path)
    return path


@pytest.fixture()
def image_folder(tmp_path):
    folder = tmp_path / "carrusel"
    colors = [(200, 60, 60), (60, 160, 90), (50, 90, 200), (180, 140, 30), (120, 60, 160)]
    names = ["10-final.png", "2-repo.jpg", "1-portada.png", "03-tercera.webp", "4-cuarta.jpeg"]
    for n, c in zip(names, colors):
        make_image(folder / n, color=c, text=n)
    return folder


class ToneTTS(TTSEngine):
    """Motor de prueba: un tono cuya duración depende del número de caracteres."""

    name = "tone"

    def __init__(self, sample_rate: int = 22050, fail_on: str | None = None):
        self.sr = sample_rate
        self.calls = 0
        self.fail_on = fail_on

    def list_voices(self):
        return [{"id": "tono", "label": "tono", "speakers": [], "sample_rate": self.sr, "ok": True}]

    def check_voice(self, voice_id):
        from tts_engine import TTSError
        if voice_id != "tono":
            raise TTSError(f"La voz «{voice_id}» no está instalada.")

    def voice_fingerprint(self, voice_id):
        return "tono-v1"

    def synthesize_sentence(self, text, params: VoiceParams):
        from tts_engine import TTSError
        if self.fail_on and self.fail_on in text:
            raise TTSError("fallo simulado")
        self.calls += 1
        dur = 0.06 * len(text) / max(0.5, params.speed)
        t = np.arange(int(dur * self.sr)) / self.sr
        audio = 0.5 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
        lead = np.zeros(int(0.2 * self.sr), dtype=np.float32)   # silencio que debe recortarse
        return np.concatenate([lead, audio, lead]), self.sr
