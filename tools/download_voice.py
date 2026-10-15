"""Descarga explícita de una voz de Piper en español desde la fuente oficial verificada.

Uso:  python tools/download_voice.py [es_ES-davefx-medium|es_ES-sharvard-medium]

Origen: https://huggingface.co/rhasspy/piper-voices (etiqueta v1.0.0). Cada archivo se
comprueba contra el tamaño y el MD5 publicados en voices.json del mismo repositorio.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_VOICE_ID, KNOWN_SPANISH_VOICES, PIPER_VOICES_BASE_URL, load_settings  # noqa: E402
from tts_engine import TTSError, download_voice  # noqa: E402


def main() -> int:
    voice = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VOICE_ID
    if voice not in KNOWN_SPANISH_VOICES:
        print(f"Voz desconocida: {voice}. Disponibles: {', '.join(KNOWN_SPANISH_VOICES)}")
        return 2
    s = load_settings()
    s.ensure_dirs()
    info = KNOWN_SPANISH_VOICES[voice]
    print(f"Voz: {voice} — {info['description']}")
    print(f"Origen: {PIPER_VOICES_BASE_URL}/{info['path']}/")
    print(f"Licencia de los datos: {info['dataset_license']}")
    print(f"Destino: {s.voices_path}")
    try:
        download_voice(voice, s.voices_path, progress=print)
    except TTSError as exc:
        print(f"ERROR: {exc}")
        return 1
    print("OK: voz descargada y verificada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
