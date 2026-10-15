"""Voz clonada con Chatterbox Multilingual (español de España). Se ejecuta en .venv-voz.

Chatterbox necesita versiones de librerías incompatibles con ShortsFactory (torch, numpy<2,
gradio 6), por eso vive en su propio entorno virtual y ShortsFactory lo usa como proceso aparte.

Modelos (licencia MIT, Resemble AI):
  * ResembleAI/Chatterbox-Multilingual-es-es → t3_es_es.safetensors  (ajuste fino «Spanish as spoken in Spain»)
  * ResembleAI/chatterbox → ve.pt, s3gen.pt, grapheme_mtl_merged_expanded_v1.json
Hugging Face verifica la suma SHA-256 de cada archivo grande al descargarlo.
Todo el audio generado lleva la marca de agua inaudible PerTh de Resemble AI.

Uso:
  python voice_clone/chatterbox_worker.py download
  python voice_clone/chatterbox_worker.py test --ref assets/voz_referencia/mi_voz.wav
  python voice_clone/chatterbox_worker.py serve        (JSON por líneas en stdin/stdout)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import wave
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = APP_DIR / "assets" / "voices" / "chatterbox"
REF_DIR = APP_DIR / "assets" / "voz_referencia"
LOG_FILE = APP_DIR / "logs" / "voz_clonada.txt"

FILES = {
    "ResembleAI/Chatterbox-Multilingual-es-es": ["t3_es_es.safetensors"],
    "ResembleAI/chatterbox": ["ve.pt", "s3gen.pt", "grapheme_mtl_merged_expanded_v1.json"],
}
T3_FILE = "t3_es_es.safetensors"
# Decodificador que acompaña al ajuste fino de España (se descarga aparte, ~1 GB)
DECODER_V3 = ("ResembleAI/Chatterbox-Multilingual-es-es", "s3gen_v3.safetensors")
LANGUAGE = "es"

_log_lines: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True) if not _SERVE else print(msg, file=sys.stderr, flush=True)
    _log_lines.append(msg)


_SERVE = False


def flush_log() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write("\n".join(_log_lines) + "\n")
    _log_lines.clear()


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------

def download_file(repo: str, name: str) -> Path:
    os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import hf_hub_download
    target = MODEL_DIR / name
    if target.is_file() and target.stat().st_size > 0:
        say(f"  {name} ya está descargado ({target.stat().st_size / 1e6:.0f} MB).")
        return target
    say(f"  Descargando {name} desde https://huggingface.co/{repo} …")
    t0 = time.time()
    path = hf_hub_download(repo_id=repo, filename=name, local_dir=str(MODEL_DIR))
    say(f"  OK {name}: {Path(path).stat().st_size / 1e6:.0f} MB en {time.time() - t0:.0f} s.")
    shutil.rmtree(MODEL_DIR / ".cache", ignore_errors=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    return Path(path)


def download() -> int:
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import hf_hub_download

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for repo, names in FILES.items():
        for name in names:
            target = MODEL_DIR / name
            if target.is_file() and target.stat().st_size > 0:
                say(f"  {name} ya está descargado ({target.stat().st_size / 1e6:.0f} MB).")
                continue
            say(f"  Descargando {name} desde https://huggingface.co/{repo} …")
            t0 = time.time()
            path = hf_hub_download(repo_id=repo, filename=name, local_dir=str(MODEL_DIR))
            say(f"  OK {name}: {Path(path).stat().st_size / 1e6:.0f} MB en {time.time() - t0:.0f} s "
                "(integridad verificada por Hugging Face).")
    cache = MODEL_DIR / ".cache"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
    return 0


# ---------------------------------------------------------------------------
# Carga del modelo (sin red)
# ---------------------------------------------------------------------------

class _NoChinese:
    """Sustituye al convertidor chino de Chatterbox, que intenta descargar recursos al iniciarse."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, text):
        return text


def load_model(device: str = "cpu", decoder: str = "v2"):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    missing = [n for names in FILES.values() for n in names if not (MODEL_DIR / n).is_file()]
    if missing:
        raise RuntimeError(f"Faltan archivos del modelo: {', '.join(missing)}. Ejecuta install_voz_clonada.bat.")
    import torch
    from safetensors.torch import load_file
    import chatterbox.models.tokenizers.tokenizer as tok_mod
    from chatterbox.models.t3 import T3
    from chatterbox.models.t3.modules.t3_config import T3Config
    from chatterbox.models.s3gen import S3Gen
    from chatterbox.models.voice_encoder import VoiceEncoder
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    tok_mod.ChineseCangjieConverter = _NoChinese
    threads = max(1, (os.cpu_count() or 2) // 2)       # núcleos físicos: rinde mejor que contar hilos SMT
    torch.set_num_threads(threads)
    cpu = torch.device("cpu")

    ve = VoiceEncoder()
    ve.load_state_dict(torch.load(MODEL_DIR / "ve.pt", map_location=cpu, weights_only=True))
    ve.to(device).eval()

    t3 = T3(T3Config.multilingual())
    state = load_file(str(MODEL_DIR / T3_FILE))
    if "model" in state.keys():
        state = state["model"][0]
    t3.load_state_dict(state)
    t3.to(device).eval()

    s3gen = S3Gen()
    if decoder == "v3":
        # El archivo v3 no incluye dos búferes fijos (banco de filtros mel y ventana STFT) que S3Gen
        # calcula al construirse; se aceptan sólo esas ausencias, cualquier otra diferencia es un error.
        missing, unexpected = s3gen.load_state_dict(load_file(str(MODEL_DIR / DECODER_V3[1])), strict=False)
        allowed = {"tokenizer._mel_filters", "tokenizer.window"}
        if unexpected or set(missing) - allowed:
            raise RuntimeError(f"Decodificador v3 incompatible: faltan {sorted(set(missing) - allowed)}, "
                               f"sobran {sorted(unexpected)[:5]}")
    else:
        s3gen.load_state_dict(torch.load(MODEL_DIR / "s3gen.pt", map_location=cpu, weights_only=True))
    s3gen.to(device).eval()

    tokenizer = tok_mod.MTLTokenizer(str(MODEL_DIR / "grapheme_mtl_merged_expanded_v1.json"))
    return ChatterboxMultilingualTTS(t3, s3gen, ve, tokenizer, device, conds=None), threads


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def prepare_reference(src: Path, dest: Path) -> float:
    """Convierte la grabación a WAV mono 24 kHz, quita silencios y normaliza el volumen con FFmpeg."""
    ffmpeg = os.environ.get("SHORTSFACTORY_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("No se encuentra FFmpeg en el PATH.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Dos pasadas: «loudnorm» encadenado detrás de «areverse» deja a FFmpeg bloqueado con algunos WAV
    # (comprobado con grabaciones de 24 kHz guardadas desde la aplicación). Por separado no ocurre.
    trim = ("highpass=f=70,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,areverse")
    tmp = dest.with_name(dest.stem + "_paso1.wav")
    tmp2 = dest.with_name(dest.stem + "_paso2.wav")
    steps = [
        ["-i", str(src), "-ac", "1", "-ar", "24000", "-af", trim, "-t", "30", str(tmp)],
        ["-i", str(tmp), "-af", "loudnorm=I=-20:TP=-2:LRA=11", "-ac", "1", "-ar", "24000", str(tmp2)],
    ]
    try:
        for args in steps:
            try:
                proc = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=120)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("FFmpeg no terminó de preparar la grabación en 120 s.") from exc
            if proc.returncode != 0:
                raise RuntimeError(f"No se pudo convertir la grabación: {proc.stderr[-500:]}")
        tmp2.replace(dest)           # sólo se sustituye la grabación preparada si todo salió bien
    finally:
        tmp.unlink(missing_ok=True)
        tmp2.unlink(missing_ok=True)
    with wave.open(str(dest), "rb") as w:
        return w.getnframes() / w.getframerate()


def save_wav(path: Path, samples, sr: int) -> float:
    import numpy as np
    a = np.asarray(samples, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(a))) or 1.0
    if peak > 0.99:
        a = a * (0.95 / peak)
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)
    return len(a) / sr


class Synth:
    def __init__(self, decoder: str = "v2"):
        self.decoder = decoder
        self.model = None
        self.threads = 0
        self.ref_key = None

    def ensure(self):
        if self.model is None:
            t0 = time.time()
            self.model, self.threads = load_model("cpu", self.decoder)
            say(f"Modelo cargado en {time.time() - t0:.1f} s (CPU, {self.threads} hilos, decodificador {self.decoder}).")

    def synth(self, text: str, ref_wav: Path, out: Path, exaggeration: float = 0.5, cfg: float = 0.5,
              temperature: float = 0.8, seed: int = 1234) -> dict:
        import torch
        self.ensure()
        key = (str(ref_wav), ref_wav.stat().st_mtime, round(exaggeration, 3))
        if key != self.ref_key:
            self.model.prepare_conditionals(str(ref_wav), exaggeration=exaggeration)
            self.ref_key = key
        t0 = time.time()
        wav = None
        for attempt in range(2):
            torch.manual_seed(seed + attempt * 7919)
            wav, finished = self._generate(text, cfg, temperature)
            if finished:
                break
            say(f"  Aviso: la frase no terminó de forma natural (intento {attempt + 1}); se reintenta con otra semilla.")
        else:
            raise RuntimeError("La voz clonada no consiguió terminar la frase sin divagar. Prueba a dividirla en "
                               "frases más cortas o usa una grabación de referencia más limpia.")
        took = time.time() - t0
        dur = save_wav(out, wav, self.model.sr)
        return {"ok": True, "out": str(out), "duration": dur, "seconds": took, "sr": self.model.sr}

    def _generate(self, text: str, cfg: float, temperature: float):
        """Igual que ChatterboxMultilingualTTS.generate, pero con un límite de tokens proporcional al texto.

        Sin él, si el modelo no emite el fin de frase sigue generando hasta 1000 tokens (40 s de audio,
        varios minutos en CPU) y acaba fallando con «index out of range». Devuelve (audio, terminó_bien).
        """
        import torch
        import torch.nn.functional as F
        from chatterbox.mtl_tts import punc_norm
        from chatterbox.models.s3tokenizer import SPEECH_VOCAB_SIZE
        m = self.model
        norm = punc_norm(text)
        # ~12 caracteres por segundo hablando; 25 tokens de voz por segundo; margen ×2 para pausas lentas
        max_tokens = int(min(1000, 25 * (2.0 * max(2.0, len(norm) / 12.0)) + 50))
        tokens = m.tokenizer.text_to_tokens(norm, language_id=LANGUAGE).to(m.device)
        tokens = torch.cat([tokens, tokens], dim=0)
        tokens = F.pad(tokens, (1, 0), value=m.t3.hp.start_text_token)
        tokens = F.pad(tokens, (0, 1), value=m.t3.hp.stop_text_token)
        with torch.inference_mode():
            speech = m.t3.inference(t3_cond=m.conds.t3, text_tokens=tokens, max_new_tokens=max_tokens,
                                    temperature=temperature, cfg_weight=cfg, repetition_penalty=2.0,
                                    min_p=0.05, top_p=1.0)[0]
            finished = bool((speech == m.t3.hp.stop_speech_token).any())
            if finished:
                speech = speech[: int((speech == m.t3.hp.stop_speech_token).nonzero()[0, 0])]
            speech = speech[speech < SPEECH_VOCAB_SIZE].to(m.device)
            if speech.numel() < 5:
                return None, False
            wav, _ = m.s3gen.inference(speech_tokens=speech, ref_dict=m.conds.gen)
            wav = wav.squeeze(0).detach().cpu().numpy()
            wav = m.watermarker.apply_watermark(wav, sample_rate=m.sr)
        return wav, finished


# ---------------------------------------------------------------------------
# Prueba
# ---------------------------------------------------------------------------

TEST_SENTENCES = [
    "Hoy te traigo cinco herramientas de GitHub que te van a ahorrar muchísimas horas.",
    "La primera organiza tus proyectos en un par de clics, y lo mejor es que funciona sin conexión.",
    "¿Cuál vas a probar primero? Guarda este vídeo y sígueme para descubrir más.",
]
VARIANTS = [("natural", 0.5, 0.5), ("expresiva", 0.7, 0.3)]


COMPARE_SENTENCES = [
    "Hoy te traigo cinco herramientas de guítjab que te van a hacer la vida mucho más sencilla.",
    "¿Sabías que con esta aplicación puedes organizar tus ideas en un par de clics? ¡Pruébala!",
]
COMPARE_CFG = [0.5, 0.8, 1.0]
COMPARE_DECODERS = ("v3", "v2")


def compare(ref_arg: str | None, decoders=COMPARE_DECODERS, cfgs=COMPARE_CFG) -> int:
    """Compara decodificador genérico (v2) y el del ajuste de España (v3) con distintos cfg."""
    say(f"==== Comparativa de voz clonada {time.strftime('%Y-%m-%d %H:%M:%S')} ====")
    src = find_reference(ref_arg)
    ref = REF_DIR / "_referencia_preparada.wav"
    dur = prepare_reference(src, ref)
    say(f"Grabación: {src.name} → {dur:.1f} s útiles.")
    try:
        download_file(*DECODER_V3)
    except Exception as exc:  # noqa: BLE001
        say(f"AVISO: no se pudo descargar el decodificador v3 ({exc}); sólo se probará el v2.")
    out_dir = APP_DIR / "muestras_voz" / "comparativa"
    for decoder in decoders:
        if decoder == "v3" and not (MODEL_DIR / DECODER_V3[1]).is_file():
            continue
        try:
            s = Synth(decoder)
            s.ensure()
        except Exception as exc:  # noqa: BLE001
            say(f"El decodificador {decoder} no es compatible con esta versión de Chatterbox: {exc}")
            continue
        for cfg in cfgs:
            for i, text in enumerate(COMPARE_SENTENCES, 1):
                name = f"dec-{decoder}_cfg{int(cfg * 10):02d}_frase{i}.wav"
                r = s.synth(text, ref, out_dir / name, exaggeration=0.5, cfg=cfg)
                say(f"  {name}: {r['duration']:.1f} s en {r['seconds']:.1f} s")
        del s
        import gc
        gc.collect()
    say(f"Muestras en: {out_dir}")
    return 0


def find_reference(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else APP_DIR / p
    exts = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus")
    cands = [p for p in sorted(REF_DIR.glob("*")) if p.suffix.lower() in exts and not p.name.startswith("_")]
    if not cands:
        raise RuntimeError(f"No hay ninguna grabación en {REF_DIR}. Guarda allí tu voz (por ejemplo mi_voz.m4a).")
    return cands[0]


def test(ref_arg: str | None) -> int:
    say(f"==== Prueba de voz clonada {time.strftime('%Y-%m-%d %H:%M:%S')} ====")
    src = find_reference(ref_arg)
    ref = REF_DIR / "_referencia_preparada.wav"
    dur = prepare_reference(src, ref)
    say(f"Grabación: {src.name} → {dur:.1f} s útiles tras quitar silencios.")
    if dur < 6:
        say("ERROR: la grabación es demasiado corta. Graba al menos 15 segundos hablando.")
        return 1
    if dur < 10:
        say("AVISO: con menos de 10 s el parecido con tu voz puede ser menor.")
    s = Synth()
    s.ensure()
    out_dir = APP_DIR / "muestras_voz"
    total_audio = total_time = 0.0
    for name, exag, cfg in VARIANTS:
        for i, text in enumerate(TEST_SENTENCES, 1):
            r = s.synth(text, ref, out_dir / f"clon_{name}_{i}.wav", exaggeration=exag, cfg=cfg)
            total_audio += r["duration"]
            total_time += r["seconds"]
            say(f"  [{name}] frase {i}: {r['duration']:.1f} s de audio en {r['seconds']:.1f} s "
                f"(x{r['seconds'] / max(0.01, r['duration']):.2f} del tiempo real)")
    say(f"Velocidad media: {total_time / max(0.01, total_audio):.2f} s de cálculo por cada segundo de voz.")
    say(f"Estimación para un Short con 40 s de voz: unos {40 * total_time / max(0.01, total_audio) / 60:.1f} min "
        "(sólo la primera vez; luego se reutiliza la caché).")
    say(f"Muestras en: {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# Servidor JSON por líneas (para ShortsFactory)
# ---------------------------------------------------------------------------

def serve(decoder: str = "v3") -> int:
    global _SERVE
    _SERVE = True
    if decoder == "v3" and not (MODEL_DIR / DECODER_V3[1]).is_file():
        decoder = "v2"
    s = Synth(decoder)
    # Canal exclusivo para el protocolo: cualquier print() de las librerías (p. ej. Perth al cargar)
    # se desvía a stderr (registro logs/voz_clonada_motor.txt) para no mezclarse con las respuestas JSON.
    out = sys.stdout
    sys.stdout = sys.stderr
    say(f"==== Motor de voz clonada {time.strftime('%Y-%m-%d %H:%M:%S')} (decodificador {decoder}) ====")
    s.ensure()                       # se carga el modelo ya, para que «listo» signifique listo de verdad
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        t0 = time.time()
        try:
            req = json.loads(line)
            say(f"[{time.strftime('%H:%M:%S')}] Orden {req.get('cmd')}: {str(req.get('text', ''))[:80]}")
            if req.get("cmd") == "quit":
                break
            if req.get("cmd") == "ping":
                resp = {"ok": True, "decoder": s.decoder}
            elif req.get("cmd") == "prepare_ref":
                dur = prepare_reference(Path(req["src"]), Path(req["out"]))
                resp = {"ok": True, "duration": dur}
            elif req.get("cmd") == "synth":
                resp = s.synth(req["text"], Path(req["ref"]), Path(req["out"]),
                               float(req.get("exaggeration", 0.5)), float(req.get("cfg", 0.5)),
                               float(req.get("temperature", 0.8)), int(req.get("seed", 1234)))
            else:
                resp = {"ok": False, "error": f"Orden desconocida: {req.get('cmd')}"}
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        say(f"[{time.strftime('%H:%M:%S')}]   → {'OK' if resp.get('ok') else 'ERROR'} en {time.time() - t0:.1f} s"
            + (f" ({resp['duration']:.1f} s de audio)" if resp.get("duration") else ""))
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "test", "compare", "serve"])
    ap.add_argument("--ref", default=None)
    ap.add_argument("--decoders", default="v3,v2")
    ap.add_argument("--decoder", default="v3", help="decodificador para el modo serve")
    ap.add_argument("--cfg", default="0.5,0.8,1.0")
    args = ap.parse_args()
    try:
        if args.cmd == "download":
            return download()
        if args.cmd == "test":
            return test(args.ref)
        if args.cmd == "compare":
            return compare(args.ref, tuple(d.strip() for d in args.decoders.split(",") if d.strip()),
                           [float(c) for c in args.cfg.split(",") if c.strip()])
        return serve(args.decoder)
    except Exception as exc:  # noqa: BLE001
        say(f"ERROR: {exc}")
        say(traceback.format_exc())
        return 1
    finally:
        if args.cmd != "serve":
            flush_log()


if __name__ == "__main__":
    sys.exit(main())
