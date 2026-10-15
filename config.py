"""Configuración general de ShortsFactory.

Los valores predeterminados se calculan respecto a la carpeta de la aplicación,
nunca respecto a rutas absolutas del equipo. El usuario puede sobrescribirlos en
``settings.json`` (no versionado) o desde la pestaña *Ajustes* de la interfaz.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# --- Privacidad: ninguna librería debe enviar telemetría -------------------------------
# onnxruntime (usado por Piper y Kokoro) envía eventos de uso a Microsoft al importarse si no se
# desactiva; Gradio y Hugging Face Hub también tienen telemetría opcional. Se apagan aquí, antes
# de que se importe cualquiera de ellas (config.py es el primer módulo que cargan todos).
for _var, _val in (("ORT_DISABLE_TELEMETRY", "1"), ("GRADIO_ANALYTICS_ENABLED", "False"),
                   ("HF_HUB_DISABLE_TELEMETRY", "1"), ("HF_HUB_OFFLINE", "1"), ("DO_NOT_TRACK", "1")):
    os.environ[_var] = _val

APP_NAME = "ShortsFactory"
APP_VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MUSIC_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus")
FONT_EXTENSIONS = (".ttf", ".otf")

# Resolución y formato de exportación predeterminados
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
PREVIEW_WIDTH = 540
PREVIEW_HEIGHT = 960
DEFAULT_FPS = 30
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

# Voces de Piper en español de España que la aplicación sabe descargar.
# Fuente oficial: repositorio rhasspy/piper-voices en Hugging Face, fijado a la
# etiqueta v1.0.0 para que las URLs sean estables y verificables.
PIPER_VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
PIPER_VOICES_JSON_URL = f"{PIPER_VOICES_BASE_URL}/voices.json"
KNOWN_SPANISH_VOICES: dict[str, dict[str, str]] = {
    "es_ES-davefx-medium": {
        "path": "es/es_ES/davefx/medium",
        "description": "Voz masculina, calidad media (22 kHz).",
        "dataset_license": "Dataset CC0 (NabuCasa voice-datasets); ajuste fino a partir de la voz lessac (en_US).",
        "model_card": "https://huggingface.co/rhasspy/piper-voices/blob/v1.0.0/es/es_ES/davefx/medium/MODEL_CARD",
    },
    "es_ES-sharvard-medium": {
        "path": "es/es_ES/sharvard/medium",
        "description": "Dos locutores (masculino y femenino), calidad media (22 kHz).",
        "dataset_license": "Dataset CC BY 3.0 (Universidad de Edimburgo): exige atribución.",
        "model_card": "https://huggingface.co/rhasspy/piper-voices/blob/v1.0.0/es/es_ES/sharvard/medium/MODEL_CARD",
    },
}
DEFAULT_VOICE_ID = "es_ES-davefx-medium"

# Kokoro-82M (licencia Apache-2.0) mediante kokoro-onnx. Archivos de la publicación oficial
# «model-files-v1.0» de github.com/thewh1teagle/kokoro-onnx. Las sumas SHA-256 se obtuvieron
# descargando esos archivos de la publicación oficial (14/10/2026) y se comprueban al descargar.
KOKORO_RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES: dict[str, dict[str, object]] = {
    "kokoro-v1.0.onnx": {"size": 325532387,
                         "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"},
    "voices-v1.0.bin": {"size": 28214398,
                        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"},
}
KOKORO_SPANISH_VOICES: dict[str, str] = {
    "ef_dora": "Dora — voz femenina, español",
    "em_alex": "Alex — voz masculina, español",
    "em_santa": "Santa — voz masculina grave, español",
}


@dataclass
class Settings:
    """Ajustes locales del equipo. Rutas vacías = valor predeterminado."""

    projects_dir: str = ""
    outputs_dir: str = ""
    temp_dir: str = ""
    logs_dir: str = ""
    assets_dir: str = ""
    voices_dir: str = ""
    cache_dir: str = ""
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    lmstudio_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = ""
    lmstudio_vision_model: str = ""
    lmstudio_timeout: float = 300.0
    server_port: int = 7860
    open_browser: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    # --- rutas resueltas -------------------------------------------------
    def _dir(self, value: str, default: str) -> Path:
        p = Path(value).expanduser() if value.strip() else APP_DIR / default
        return p if p.is_absolute() else (APP_DIR / p)

    @property
    def projects_path(self) -> Path:
        return self._dir(self.projects_dir, "projects")

    @property
    def outputs_path(self) -> Path:
        return self._dir(self.outputs_dir, "outputs")

    @property
    def temp_path(self) -> Path:
        return self._dir(self.temp_dir, "temp")

    @property
    def logs_path(self) -> Path:
        return self._dir(self.logs_dir, "logs")

    @property
    def assets_path(self) -> Path:
        return self._dir(self.assets_dir, "assets")

    @property
    def cache_path(self) -> Path:
        """Caché persistente (audios de voz y clips). Puede borrarse sin perder proyectos."""
        return self._dir(self.cache_dir, "cache")

    @property
    def fonts_path(self) -> Path:
        return self.assets_path / "fonts"

    @property
    def music_path(self) -> Path:
        return self.assets_path / "music"

    @property
    def voices_path(self) -> Path:
        if self.voices_dir.strip():
            return self._dir(self.voices_dir, "assets/voices")
        return self.assets_path / "voices"

    def ensure_dirs(self) -> None:
        for p in (self.projects_path, self.outputs_path, self.temp_path, self.logs_path, self.cache_path,
                  self.fonts_path, self.music_path, self.voices_path):
            p.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        s = cls(**clean)
        try:
            s.lmstudio_timeout = float(s.lmstudio_timeout)
        except (TypeError, ValueError):
            s.lmstudio_timeout = 300.0
        try:
            s.server_port = int(s.server_port)
        except (TypeError, ValueError):
            s.server_port = 7860
        return s


_settings_lock = threading.Lock()
_settings: Settings | None = None


def load_settings(path: Path | None = None) -> Settings:
    global _settings
    path = path or SETTINGS_FILE
    with _settings_lock:
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logging.getLogger(__name__).warning(
                    "No se pudo leer %s (%s). Se usan los valores predeterminados.", path, exc)
        _settings = Settings.from_dict(data)
        return _settings


def get_settings() -> Settings:
    return _settings if _settings is not None else load_settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    global _settings
    path = path or SETTINGS_FILE
    with _settings_lock:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        _settings = settings


def set_settings_for_tests(settings: Settings) -> None:
    global _settings
    _settings = settings


# --- localización de FFmpeg -------------------------------------------------

def find_executable(name: str, configured: str = "") -> str | None:
    """Busca un ejecutable: primero la ruta configurada, después el PATH."""
    if configured and configured.strip():
        p = Path(configured.strip().strip('"')).expanduser()
        if p.is_dir():
            for cand in (p / f"{name}.exe", p / name, p / "bin" / f"{name}.exe", p / "bin" / name):
                if cand.is_file():
                    return str(cand)
            return None
        return str(p) if p.is_file() else None
    found = shutil.which(name)
    return found


def ffmpeg_exe(settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    return find_executable("ffmpeg", s.ffmpeg_path)


def ffprobe_exe(settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    if s.ffprobe_path.strip():
        return find_executable("ffprobe", s.ffprobe_path)
    # Si sólo se configuró ffmpeg, buscar ffprobe a su lado
    if s.ffmpeg_path.strip():
        ff = find_executable("ffmpeg", s.ffmpeg_path)
        if ff:
            sib = Path(ff).with_name("ffprobe.exe" if ff.lower().endswith(".exe") else "ffprobe")
            if sib.is_file():
                return str(sib)
    return shutil.which("ffprobe")


# --- registro ---------------------------------------------------------------

_logging_ready = False


def setup_logging(settings: Settings | None = None, level: int = logging.INFO) -> Path:
    """Configura el registro en consola y en ``logs/shortsfactory.log`` (rotativo)."""
    global _logging_ready
    s = settings or get_settings()
    s.logs_path.mkdir(parents=True, exist_ok=True)
    log_file = s.logs_path / "shortsfactory.log"
    if _logging_ready:
        return log_file
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "urllib3", "gradio", "matplotlib", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _logging_ready = True
    return log_file
