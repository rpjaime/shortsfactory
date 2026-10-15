"""Síntesis de voz local con proveedores intercambiables.

El primer proveedor es Piper (paquete ``piper-tts``, proyecto OHF-Voice/piper1-gpl).
Cada fragmento se sintetiza frase a frase, de modo que se conoce la duración real
de cada frase y los subtítulos pueden sincronizarse con el audio real.

La caché reutiliza el audio si no cambian el texto, la voz (incluido su archivo de
modelo) ni los parámetros.
"""
from __future__ import annotations

import abc
import atexit
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

import config  # noqa: F401  (desactiva la telemetría de onnxruntime antes de importarlo)
from config import (KNOWN_SPANISH_VOICES, KOKORO_FILES, KOKORO_RELEASE_URL, KOKORO_SPANISH_VOICES,
                    PIPER_VOICES_BASE_URL, PIPER_VOICES_JSON_URL, get_settings)
from pronunciation import apply_lexicon

log = logging.getLogger(__name__)

TRIM_THRESHOLD_DB = -45.0
TRIM_KEEP_SECONDS = 0.04
MIN_SPEED, MAX_SPEED = 0.85, 1.15   # límites para no acelerar ni frenar la voz de forma agresiva


class TTSError(Exception):
    """Error comprensible de síntesis de voz."""


@dataclass
class Segment:
    text: str
    start: float
    end: float


@dataclass
class SynthesisResult:
    wav_path: str
    duration: float
    sample_rate: int
    segments: list[Segment] = field(default_factory=list)
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["segments"] = [asdict(s) for s in self.segments]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SynthesisResult":
        return cls(d["wav_path"], float(d["duration"]), int(d["sample_rate"]),
                   [Segment(**s) for s in d.get("segments", [])], bool(d.get("cached", False)))


@dataclass
class VoiceParams:
    voice_id: str
    speed: float = 1.0
    sentence_pause: float = 0.25
    speaker_id: int | None = None
    noise_scale: float | None = None
    noise_w_scale: float | None = None

    @classmethod
    def from_project(cls, v: dict[str, Any]) -> "VoiceParams":
        sid = v.get("speaker_id")
        return cls(
            voice_id=v.get("voice_id", ""),
            speed=float(v.get("speed", 1.0) or 1.0),
            sentence_pause=float(v.get("sentence_pause", 0.25) or 0.0),
            speaker_id=int(sid) if sid not in (None, "", "None") else None,
            noise_scale=v.get("noise_scale"),
            noise_w_scale=v.get("noise_w_scale"),
        )


# ---------------------------------------------------------------------------
# Segmentación en frases (español)
# ---------------------------------------------------------------------------

_ABBREV = {"sr", "sra", "srta", "dr", "dra", "ud", "uds", "etc", "p. ej", "ej", "pág", "núm", "aprox",
           "vs", "approx", "e.g", "i.e", "art", "cap", "fig", "min", "máx", "mín"}


def split_sentences(text: str) -> list[str]:
    """Divide en frases respetando abreviaturas, decimales y signos ¿¡."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    out: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ".!?…":
            j = i
            while j + 1 < n and text[j + 1] in ".!?…\"'»)”":
                j += 1
            nxt = text[j + 1] if j + 1 < n else ""
            if ch == "." and i > 0 and text[i - 1].isdigit() and nxt.isdigit():
                i += 1
                continue  # decimal
            if ch == ".":
                word = re.findall(r"([\wáéíóúñü.]+)$", text[start:i].lower())
                last = word[0].rstrip(".").split(".")[-1] if word else ""
                if word and (word[0].rstrip(".") in _ABBREV or last in _ABBREV
                             or (len(last) == 1 and last.isalpha())):
                    i = j + 1
                    continue
            if nxt == "" or nxt == " ":
                sent = text[start:j + 1].strip()
                if sent:
                    out.append(sent)
                start = j + 1
                i = j + 1
                continue
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


# ---------------------------------------------------------------------------
# Utilidades de audio
# ---------------------------------------------------------------------------

def trim_silence(audio: np.ndarray, sample_rate: int, threshold_db: float = TRIM_THRESHOLD_DB,
                 keep: float = TRIM_KEEP_SECONDS) -> np.ndarray:
    """Recorta silencios iniciales y finales (conserva un pequeño margen)."""
    if audio.size == 0:
        return audio
    thr = 10 ** (threshold_db / 20.0)
    idx = np.flatnonzero(np.abs(audio) > thr)
    if idx.size == 0:
        return audio[:0]
    k = int(keep * sample_rate)
    a = max(0, idx[0] - k)
    b = min(audio.size, idx[-1] + 1 + k)
    return audio[a:b]


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def read_wav_duration(path: str | Path) -> float:
    """Duración real del WAV; detecta archivos truncados comparando cabecera y tamaño."""
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = float(w.getframerate())
        bytes_per_frame = w.getsampwidth() * w.getnchannels()
    data_bytes = Path(path).stat().st_size - 44
    if data_bytes < frames * bytes_per_frame - 2:
        raise TTSError(f"El archivo de audio {Path(path).name} está incompleto.")
    return frames / rate


def _safe_duration(path: Path) -> float:
    try:
        return read_wav_duration(path)
    except Exception:  # noqa: BLE001  (archivo dañado o a medio escribir)
        return 0.0


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise TTSError(f"WAV con {sw * 8} bits no soportado (se esperaba 16 bits).")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


# ---------------------------------------------------------------------------
# Interfaz común
# ---------------------------------------------------------------------------

class TTSEngine(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def list_voices(self) -> list[dict[str, Any]]:
        """[{'id', 'label', 'speakers': [..], 'sample_rate'}]"""

    @abc.abstractmethod
    def check_voice(self, voice_id: str) -> None:
        """Lanza TTSError si la voz no está instalada o no es compatible."""

    @abc.abstractmethod
    def voice_fingerprint(self, voice_id: str) -> str:
        """Identificador que cambia si cambia el modelo de voz."""

    @abc.abstractmethod
    def synthesize_sentence(self, text: str, params: VoiceParams) -> tuple[np.ndarray, int]:
        """Sintetiza una frase y devuelve (audio float32 mono, sample_rate)."""

    def synthesize(self, text: str, params: VoiceParams, out_wav: Path,
                   lexicon: list[tuple[str, str]] | None = None) -> SynthesisResult:
        """Sintetiza frase a frase. El diccionario de pronunciaciones sólo afecta al audio:
        los segmentos (subtítulos) conservan el texto original."""
        sentences = split_sentences(text)
        if not sentences:
            raise TTSError("No hay texto que sintetizar.")
        self.check_voice(params.voice_id)
        pieces: list[np.ndarray] = []
        segments: list[Segment] = []
        sr = 0
        t = 0.0
        for k, sent in enumerate(sentences):
            audio, sr_k = self.synthesize_sentence(apply_lexicon(sent, lexicon), params)
            if sr and sr_k != sr:
                raise TTSError("La voz devolvió frecuencias de muestreo distintas entre frases.")
            sr = sr_k
            audio = trim_silence(audio.astype(np.float32), sr)
            if audio.size == 0:
                log.warning("La frase %r no produjo audio audible.", sent)
                continue
            if pieces and params.sentence_pause > 0:
                gap = np.zeros(int(round(params.sentence_pause * sr)), dtype=np.float32)
                pieces.append(gap)
                t += gap.size / sr
            dur = audio.size / sr
            segments.append(Segment(sent, round(t, 4), round(t + dur, 4)))
            pieces.append(audio)
            t += dur
        if not pieces:
            raise TTSError("La voz no generó audio audible para este texto.")
        full = np.concatenate(pieces)
        peak = float(np.max(np.abs(full))) or 1.0
        full = full * (0.89 / peak)  # -1 dBFS de pico
        write_wav(out_wav, full, sr)
        return SynthesisResult(str(out_wav), full.size / sr, sr, segments)


# ---------------------------------------------------------------------------
# Piper
# ---------------------------------------------------------------------------

def _file_fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}".encode())
    with open(path, "rb") as fh:
        h.update(fh.read(1_048_576))
    return h.hexdigest()[:16]


class PiperEngine(TTSEngine):
    name = "piper"

    def __init__(self, voices_dir: Path | None = None):
        self.voices_dir = Path(voices_dir) if voices_dir else get_settings().voices_path
        self._loaded: dict[str, Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def is_available() -> tuple[bool, str]:
        try:
            import piper  # noqa: F401
            from piper import PiperVoice  # noqa: F401
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, (f"Piper no está instalado o no se puede cargar ({exc}). "
                           "Ejecuta install.bat para instalar las dependencias.")

    def _paths(self, voice_id: str) -> tuple[Path, Path]:
        if not voice_id or not re.match(r"^[A-Za-z0-9_.\-]+$", voice_id):
            raise TTSError(f"Identificador de voz no válido: {voice_id!r}")
        model = self.voices_dir / f"{voice_id}.onnx"
        return model, Path(str(model) + ".json")

    def list_voices(self) -> list[dict[str, Any]]:
        out = []
        if not self.voices_dir.is_dir():
            return out
        for model in sorted(self.voices_dir.glob("*.onnx")):
            vid = model.stem
            cfg = Path(str(model) + ".json")
            entry = {"id": vid, "label": vid, "speakers": [], "sample_rate": 0, "ok": False, "error": ""}
            try:
                data = self._read_config(cfg)
                entry["sample_rate"] = data["audio"]["sample_rate"]
                entry["speakers"] = sorted(data.get("speaker_id_map", {}).items(), key=lambda kv: kv[1])
                lang = data.get("language", {}).get("code") or data.get("espeak", {}).get("voice", "")
                entry["label"] = f"{vid} ({lang}, {entry['sample_rate']} Hz)"
                entry["ok"] = True
            except TTSError as exc:
                entry["error"] = str(exc)
                entry["label"] = f"{vid} — NO VÁLIDA"
            out.append(entry)
        return out

    @staticmethod
    def _read_config(cfg: Path) -> dict[str, Any]:
        if not cfg.is_file():
            raise TTSError(f"Falta el archivo de configuración de la voz: {cfg.name}")
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TTSError(f"El archivo {cfg.name} está dañado o no es JSON: {exc}") from exc
        for key in ("audio", "phoneme_id_map", "num_speakers"):
            if key not in data:
                raise TTSError(f"{cfg.name} no es una configuración de voz de Piper (falta «{key}»).")
        if not isinstance(data["audio"], dict) or "sample_rate" not in data["audio"]:
            raise TTSError(f"{cfg.name}: falta audio.sample_rate.")
        if data.get("phoneme_type", "espeak") == "espeak" and "voice" not in data.get("espeak", {}):
            raise TTSError(f"{cfg.name}: falta espeak.voice.")
        return data

    def check_voice(self, voice_id: str) -> None:
        ok, msg = self.is_available()
        if not ok:
            raise TTSError(msg)
        model, cfg = self._paths(voice_id)
        if not model.is_file():
            raise TTSError(
                f"La voz «{voice_id}» no está instalada (no existe {model}). Descárgala en la pestaña Voz "
                "o con install.bat.")
        if model.stat().st_size < 1_000_000:
            raise TTSError(f"El modelo {model.name} es demasiado pequeño: la descarga parece incompleta.")
        self._read_config(cfg)

    def voice_fingerprint(self, voice_id: str) -> str:
        model, cfg = self._paths(voice_id)
        return _file_fingerprint(model) + _file_fingerprint(cfg)[:8]

    def _load(self, voice_id: str):
        with self._lock:
            if voice_id in self._loaded:
                return self._loaded[voice_id]
            self.check_voice(voice_id)
            from piper import PiperVoice
            model, cfg = self._paths(voice_id)
            try:
                voice = PiperVoice.load(str(model), config_path=str(cfg), use_cuda=False)
            except Exception as exc:  # noqa: BLE001
                raise TTSError(
                    f"No se pudo cargar la voz «{voice_id}»: el modelo es incompatible o está dañado ({exc}). "
                    "Vuelve a descargarla.") from exc
            self._loaded[voice_id] = voice
            return voice

    def synthesize_sentence(self, text: str, params: VoiceParams) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig
        voice = self._load(params.voice_id)
        speed = min(MAX_SPEED, max(MIN_SPEED, params.speed or 1.0))
        num_speakers = getattr(voice.config, "num_speakers", 1)
        sid = params.speaker_id if (params.speaker_id is not None and num_speakers > 1) else None
        if sid is not None and not 0 <= sid < num_speakers:
            raise TTSError(f"La voz tiene {num_speakers} locutores; el locutor {sid} no existe.")
        cfg = SynthesisConfig(speaker_id=sid, length_scale=1.0 / speed,
                              noise_scale=params.noise_scale, noise_w_scale=params.noise_w_scale,
                              normalize_audio=True, volume=1.0)
        chunks = []
        sr = voice.config.sample_rate
        try:
            for chunk in voice.synthesize(text, syn_config=cfg):
                chunks.append(np.asarray(chunk.audio_float_array, dtype=np.float32))
                sr = chunk.sample_rate
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"Piper falló al sintetizar «{text[:60]}»: {exc}") from exc
        if not chunks:
            return np.zeros(0, dtype=np.float32), sr
        return np.concatenate(chunks), sr


# ---------------------------------------------------------------------------
# Caché
# ---------------------------------------------------------------------------

class NarrationCache:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else get_settings().cache_path / "tts"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(engine: TTSEngine, params: VoiceParams, text: str,
            lexicon: list[tuple[str, str]] | None = None) -> str:
        norm = re.sub(r"\s+", " ", text.strip())
        spoken = [apply_lexicon(s, lexicon) for s in split_sentences(norm)]
        payload = json.dumps({
            "engine": engine.name, "fp": engine.voice_fingerprint(params.voice_id),
            "params": asdict(params), "text": norm, "spoken": spoken, "v": 3,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def get(self, key: str) -> SynthesisResult | None:
        meta = self.root / f"{key}.json"
        wav = self.root / f"{key}.wav"
        if not (meta.is_file() and wav.is_file()):
            return None
        try:
            res = SynthesisResult.from_dict(json.loads(meta.read_text(encoding="utf-8")))
            real = read_wav_duration(wav)
            if abs(real - res.duration) > 0.02 or real <= 0:
                return None  # archivo incompleto o alterado
            res.wav_path = str(wav)
            res.cached = True
            return res
        except Exception:  # noqa: BLE001
            return None

    def put(self, key: str, res: SynthesisResult) -> None:
        meta = self.root / f"{key}.json"
        tmp = meta.with_suffix(".json.part")
        tmp.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(meta)

    def synthesize(self, engine: TTSEngine, text: str, params: VoiceParams,
                   lexicon: list[tuple[str, str]] | None = None) -> SynthesisResult:
        k = self.key(engine, params, text, lexicon)
        hit = self.get(k)
        if hit is not None:
            return hit
        res = engine.synthesize(text, params, self.root / f"{k}.wav", lexicon)
        self.put(k, res)
        return res


class KokoroEngine(TTSEngine):
    """Kokoro-82M (Apache-2.0) mediante kokoro-onnx; voces en español: ef_dora, em_alex, em_santa."""

    name = "kokoro"
    SAMPLE_RATE = 24000

    def __init__(self, model_dir: Path | None = None):
        self.model_dir = Path(model_dir) if model_dir else get_settings().voices_path / "kokoro"
        self._model = None
        self._lock = threading.Lock()

    @staticmethod
    def is_available() -> tuple[bool, str]:
        try:
            import kokoro_onnx  # noqa: F401
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, (f"kokoro-onnx no está instalado ({exc}). Ejecuta install.bat para actualizar "
                           "las dependencias.")

    def _files(self) -> tuple[Path, Path]:
        return self.model_dir / "kokoro-v1.0.onnx", self.model_dir / "voices-v1.0.bin"

    def models_installed(self) -> bool:
        model, voices = self._files()
        return all(p.is_file() and p.stat().st_size == KOKORO_FILES[p.name]["size"] for p in (model, voices))

    def list_voices(self) -> list[dict[str, Any]]:
        if not self.models_installed():
            return []
        return [{"id": vid, "label": f"Kokoro · {desc}", "speakers": [], "sample_rate": self.SAMPLE_RATE,
                 "ok": True, "error": ""} for vid, desc in KOKORO_SPANISH_VOICES.items()]

    def check_voice(self, voice_id: str) -> None:
        ok, msg = self.is_available()
        if not ok:
            raise TTSError(msg)
        if voice_id not in KOKORO_SPANISH_VOICES:
            raise TTSError(f"La voz Kokoro «{voice_id}» no es una voz española disponible "
                           f"({', '.join(KOKORO_SPANISH_VOICES)}).")
        if not self.models_installed():
            raise TTSError("Los archivos del modelo Kokoro no están instalados o están incompletos. "
                           "Descárgalos en la pestaña Voz.")

    def voice_fingerprint(self, voice_id: str) -> str:
        model, voices = self._files()
        return f"kokoro-v1.0:{voice_id}:{model.stat().st_size}:{voices.stat().st_size}"

    def _load(self):
        with self._lock:
            if self._model is None:
                from kokoro_onnx import Kokoro
                model, voices = self._files()
                try:
                    self._model = Kokoro(str(model), str(voices))
                except Exception as exc:  # noqa: BLE001
                    raise TTSError(f"No se pudo cargar el modelo Kokoro ({exc}). Vuelve a descargarlo.") from exc
            return self._model

    def synthesize_sentence(self, text: str, params: VoiceParams) -> tuple[np.ndarray, int]:
        self.check_voice(params.voice_id)
        model = self._load()
        speed = min(MAX_SPEED, max(MIN_SPEED, params.speed or 1.0))
        try:
            with self._lock:
                samples, sr = model.create(text, voice=params.voice_id, speed=speed, lang="es")
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"Kokoro falló al sintetizar «{text[:60]}»: {exc}") from exc
        return np.asarray(samples, dtype=np.float32), int(sr)


class ChatterboxEngine(TTSEngine):
    """Voz clonada con Chatterbox Multilingual (ajuste fino de España). Corre en .venv-voz como proceso
    aparte (voice_clone/chatterbox_worker.py serve) que se mantiene abierto mientras dura la sesión.

    Cada grabación de assets/voz_referencia es una «voz». Ajustes fijados tras las pruebas de audio:
    decodificador v3 (el del ajuste de España), cfg 0,8 (hereda más el acento de la referencia) y
    exageración 0,5. Chatterbox no admite variar la velocidad.
    """

    name = "chatterbox"
    CFG = 0.8
    EXAGGERATION = 0.5
    DECODER = "v3"
    AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus")
    MODEL_FILES = ("t3_es_es.safetensors", "ve.pt", "grapheme_mtl_merged_expanded_v1.json")
    START_TIMEOUT = 300.0
    SENTENCE_TIMEOUT = 600.0

    def __init__(self, app_dir: Path | None = None, python_exe: str | None = None,
                 worker: Path | None = None):
        from config import APP_DIR
        self.app_dir = Path(app_dir) if app_dir else APP_DIR
        venv = self.app_dir / ".venv-voz"
        default_py = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        self.python_exe = python_exe or str(default_py)
        self.worker = Path(worker) if worker else self.app_dir / "voice_clone" / "chatterbox_worker.py"
        self.model_dir = self.app_dir / "assets" / "voices" / "chatterbox"
        self.ref_dir = self.app_dir / "assets" / "voz_referencia"
        self.out_dir = get_settings().cache_path / "chatterbox_tmp"
        self.engine_log = get_settings().logs_path / "voz_clonada_motor.txt"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._counter = 0

    # -- disponibilidad --
    def is_available(self) -> tuple[bool, str]:
        if not Path(self.python_exe).is_file():
            return False, "La voz clonada no está instalada: ejecuta install_voz_clonada.bat."
        if not self.worker.is_file():
            return False, f"Falta {self.worker}: vuelve a copiar ShortsFactory o ejecuta install_voz_clonada.bat."
        missing = [f for f in self.MODEL_FILES if not (self.model_dir / f).is_file()]
        if not ((self.model_dir / "s3gen_v3.safetensors").is_file() or (self.model_dir / "s3gen.pt").is_file()):
            missing.append("s3gen_v3.safetensors")
        if missing:
            return False, f"Faltan modelos de la voz clonada ({', '.join(missing)}): ejecuta install_voz_clonada.bat."
        return True, ""

    def references(self) -> list[Path]:
        if not self.ref_dir.is_dir():
            return []
        return [p for p in sorted(self.ref_dir.iterdir())
                if p.is_file() and p.suffix.lower() in self.AUDIO_EXTS and not p.name.startswith("_")]

    def list_voices(self) -> list[dict[str, Any]]:
        if not self.is_available()[0]:
            return []
        return [{"id": p.stem, "label": f"Voz clonada · {p.stem}", "speakers": [], "sample_rate": 24000,
                 "ok": True, "error": ""} for p in self.references()]

    def _ref_source(self, voice_id: str) -> Path:
        for p in self.references():
            if p.stem == voice_id:
                return p
        raise TTSError(f"No existe la grabación de referencia «{voice_id}» en {self.ref_dir}.")

    def check_voice(self, voice_id: str) -> None:
        ok, msg = self.is_available()
        if not ok:
            raise TTSError(msg)
        self._ref_source(voice_id)

    def voice_fingerprint(self, voice_id: str) -> str:
        src = self._ref_source(voice_id)
        return f"chatterbox-es-es:{self.DECODER}:cfg{self.CFG}:ex{self.EXAGGERATION}:{_file_fingerprint(src)}"

    # -- proceso de trabajo --
    def _start(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1")
        ff = config.ffmpeg_exe()
        if ff:
            env["SHORTSFACTORY_FFMPEG"] = ff
        log.info("Iniciando el motor de voz clonada (carga del modelo, unos 30-60 s)…")
        t0 = time.time()
        self.engine_log.parent.mkdir(parents=True, exist_ok=True)
        err = open(self.engine_log, "a", encoding="utf-8", errors="replace")
        try:
            self._proc = subprocess.Popen(
                [self.python_exe, "-u", str(self.worker), "serve", "--decoder", self.DECODER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                encoding="utf-8", errors="replace", cwd=str(self.app_dir), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            err.close()
        first = self._readline(self.START_TIMEOUT)
        if '"ready"' not in first:
            self.close()
            raise TTSError(f"El motor de voz clonada no arrancó: {first[:300] or 'sin respuesta'}. "
                           f"Detalles en {self.engine_log}")
        log.info("Motor de voz clonada listo en %.0f s.", time.time() - t0)
        atexit.register(self.close)
        return self._proc

    def _readline(self, timeout: float) -> str:
        """Lee la siguiente respuesta JSON del worker; descarta líneas de texto que no lo sean."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        result: list[str] = []

        def reader():
            while True:
                line = stdout.readline()
                if not line:
                    result.append("")
                    return
                line = line.strip()
                if line.startswith("{"):
                    result.append(line)
                    return
                log.debug("Voz clonada (texto ignorado): %s", line[:200])

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.close()
            raise TTSError(f"El motor de voz clonada no respondió en {timeout:.0f} s.")
        return result[0] if result else ""

    def _request(self, payload: dict, timeout: float) -> dict:
        proc = self._start()
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            self.close()
            raise TTSError(f"Se perdió la conexión con el motor de voz clonada: {exc}") from exc
        line = self._readline(timeout)
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            self.close()
            raise TTSError(f"Respuesta no válida del motor de voz clonada: {line[:200]}") from exc
        if not resp.get("ok"):
            raise TTSError(f"La voz clonada falló: {resp.get('error', 'error desconocido')}")
        return resp

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write('{"cmd": "quit"}\n')
                    proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()

    def _prepared_ref(self, voice_id: str) -> Path:
        src = self._ref_source(voice_id)
        prep = self.ref_dir / f"_preparada_{voice_id}.wav"
        if not prep.is_file() or prep.stat().st_mtime < src.stat().st_mtime or _safe_duration(prep) < 1:
            resp = self._request({"cmd": "prepare_ref", "src": str(src), "out": str(prep)}, 120)
            if resp.get("duration", 0) < 6:
                raise TTSError("La grabación de referencia es demasiado corta (mínimo 6 s útiles; mejor 15-30 s).")
        return prep

    def synthesize_sentence(self, text: str, params: VoiceParams) -> tuple[np.ndarray, int]:
        with self._lock:
            ref = self._prepared_ref(params.voice_id)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._counter += 1
            out = self.out_dir / f"frase_{os.getpid()}_{self._counter}.wav"
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
            log.info("Voz clonada: generando «%s»…", text[:60])
            resp = self._request({"cmd": "synth", "text": text, "ref": str(ref), "out": str(out), "cfg": self.CFG,
                           "exaggeration": self.EXAGGERATION, "seed": seed}, self.SENTENCE_TIMEOUT)
            log.info("Voz clonada: %.1f s de audio en %.0f s.", resp.get("duration", 0), resp.get("seconds", 0))
            try:
                audio, sr = read_wav(out)
            finally:
                out.unlink(missing_ok=True)
            return audio, sr


ENGINE_LABELS = {"piper": "Piper", "kokoro": "Kokoro", "chatterbox": "Voz clonada"}
_ENGINES: dict[str, TTSEngine] = {}


def get_engine(name: str = "piper") -> TTSEngine:
    if name not in ENGINE_LABELS:
        raise TTSError(f"Motor de voz desconocido: {name}")
    if name not in _ENGINES:
        _ENGINES[name] = {"piper": PiperEngine, "kokoro": KokoroEngine, "chatterbox": ChatterboxEngine}[name]()
    eng = _ENGINES[name]
    if isinstance(eng, PiperEngine):
        eng.voices_dir = get_settings().voices_path
    elif isinstance(eng, KokoroEngine):
        eng.model_dir = get_settings().voices_path / "kokoro"
    return eng


def all_voices() -> list[dict[str, Any]]:
    """Voces instaladas de todos los motores: [{'engine', 'id', 'label', ...}]."""
    out = []
    for name in ENGINE_LABELS:
        try:
            for v in get_engine(name).list_voices():
                out.append({**v, "engine": name})
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudieron listar las voces de %s: %s", name, exc)
    return out


def download_kokoro(dest_dir: Path | None = None, progress: Callable[[str], None] | None = None,
                    timeout: float = 60.0) -> Path:
    """Descarga Kokoro-82M desde la publicación oficial verificando tamaño y SHA-256."""
    say = progress or (lambda m: log.info(m))
    dest = Path(dest_dir) if dest_dir else get_settings().voices_path / "kokoro"
    dest.mkdir(parents=True, exist_ok=True)
    for name, meta in KOKORO_FILES.items():
        target = dest / name
        if target.is_file() and target.stat().st_size == meta["size"] and _sha256(target) == meta["sha256"]:
            say(f"{name} ya está descargado y verificado.")
            continue
        url = f"{KOKORO_RELEASE_URL}/{name}"
        part = target.with_name(name + ".part")
        say(f"Descargando {url} ({int(meta['size']) / 1e6:.0f} MB)…")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r, open(part, "wb") as fh:
                for buf in iter(lambda: r.read(1 << 20), b""):
                    fh.write(buf)
        except Exception as exc:  # noqa: BLE001
            part.unlink(missing_ok=True)
            raise TTSError(f"Fallo al descargar {url}: {exc}") from exc
        if part.stat().st_size != meta["size"] or _sha256(part) != meta["sha256"]:
            part.unlink(missing_ok=True)
            raise TTSError(f"La descarga de {name} no supera la verificación (tamaño/SHA-256).")
        part.replace(target)
        say(f"{name} verificado (SHA-256 {meta['sha256'][:16]}…).")
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Descarga explícita de voces (sólo desde la fuente oficial fijada)
# ---------------------------------------------------------------------------

def download_voice(voice_id: str, dest_dir: Path | None = None,
                   progress: Callable[[str], None] | None = None, timeout: float = 60.0) -> Path:
    """Descarga una voz de rhasspy/piper-voices (etiqueta v1.0.0) verificando tamaño y MD5."""
    say = progress or (lambda m: log.info(m))
    if voice_id not in KNOWN_SPANISH_VOICES:
        raise TTSError(f"La voz {voice_id!r} no está en la lista de voces verificadas.")
    dest = Path(dest_dir) if dest_dir else get_settings().voices_path
    dest.mkdir(parents=True, exist_ok=True)
    rel = KNOWN_SPANISH_VOICES[voice_id]["path"]
    say(f"Consultando el índice oficial de voces: {PIPER_VOICES_JSON_URL}")
    try:
        with urllib.request.urlopen(PIPER_VOICES_JSON_URL, timeout=timeout) as r:
            index = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"No se pudo descargar el índice de voces ({exc}). Comprueba la conexión a Internet.") from exc
    entry = index.get(voice_id)
    if not entry or "files" not in entry:
        raise TTSError(f"La voz {voice_id} no aparece en el índice oficial.")
    for ext in (".onnx", ".onnx.json"):
        rel_file = f"{rel}/{voice_id}{ext}"
        meta = entry["files"].get(rel_file)
        if not meta:
            raise TTSError(f"El índice no incluye {rel_file}.")
        url = f"{PIPER_VOICES_BASE_URL}/{rel_file}"
        target = dest / f"{voice_id}{ext}"
        if target.is_file() and target.stat().st_size == meta["size_bytes"] and \
                _md5(target) == meta["md5_digest"]:
            say(f"{target.name} ya está descargado y verificado.")
            continue
        part = target.with_name(target.name + ".part")
        say(f"Descargando {url} ({meta['size_bytes'] / 1e6:.1f} MB)…")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r, open(part, "wb") as fh:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    fh.write(buf)
        except Exception as exc:  # noqa: BLE001
            part.unlink(missing_ok=True)
            raise TTSError(f"Fallo al descargar {url}: {exc}") from exc
        size = part.stat().st_size
        md5 = _md5(part)
        if size != meta["size_bytes"] or md5 != meta["md5_digest"]:
            part.unlink(missing_ok=True)
            raise TTSError(f"La descarga de {target.name} no supera la verificación (tamaño/MD5). Inténtalo de nuevo.")
        part.replace(target)
        say(f"{target.name} verificado (MD5 {md5}).")
    return dest / f"{voice_id}.onnx"


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - el índice oficial publica MD5
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()
