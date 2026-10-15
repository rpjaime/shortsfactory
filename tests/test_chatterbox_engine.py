import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import ROOT, needs_ffmpeg
from tts_engine import ChatterboxEngine, NarrationCache, TTSError, VoiceParams


@pytest.fixture()
def fake_app(tmp_path, settings):
    app = tmp_path / "app"
    md = app / "assets" / "voices" / "chatterbox"
    md.mkdir(parents=True)
    for f in ("t3_es_es.safetensors", "ve.pt", "grapheme_mtl_merged_expanded_v1.json", "s3gen_v3.safetensors"):
        (md / f).write_bytes(b"x")
    ref = app / "assets" / "voz_referencia"
    ref.mkdir(parents=True)
    with wave.open(str(ref / "mi_voz.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes((np.sin(np.arange(24000 * 12) / 10) * 8000).astype("<i2").tobytes())
    worker = Path(__file__).parent / "fake_chatterbox_worker.py"
    eng = ChatterboxEngine(app_dir=app, python_exe=sys.executable, worker=worker)
    yield eng
    eng.close()


def test_lists_reference_recordings_as_voices(fake_app):
    voices = fake_app.list_voices()
    assert [v["id"] for v in voices] == ["mi_voz"] and voices[0]["label"].startswith("Voz clonada")


def test_synthesis_through_worker_with_lexicon(fake_app):
    res = NarrationCache().synthesize(fake_app, "Mira GitHub. Es útil.", VoiceParams("mi_voz"),
                                      [("GitHub", "guítjab")])
    assert len(res.segments) == 2 and res.segments[0].text == "Mira GitHub."
    assert res.duration > 0.5 and res.sample_rate == 24000
    assert (fake_app.ref_dir / "_preparada_mi_voz.wav").is_file()          # referencia preparada y reutilizada
    again = NarrationCache().synthesize(fake_app, "Mira GitHub. Es útil.", VoiceParams("mi_voz"),
                                        [("GitHub", "guítjab")])
    assert again.cached


def test_clear_errors(fake_app, tmp_path):
    with pytest.raises(TTSError, match="No existe la grabación"):
        fake_app.check_voice("otra_voz")
    with pytest.raises(TTSError, match="fallo simulado"):
        fake_app.synthesize("Esto FALLA.", VoiceParams("mi_voz"), tmp_path / "x.wav")
    missing = ChatterboxEngine(app_dir=tmp_path / "vacia", python_exe=sys.executable)
    ok, msg = missing.is_available()
    assert not ok and "install_voz_clonada.bat" in msg
    assert missing.list_voices() == []


@needs_ffmpeg
def test_prepare_reference_does_not_hang(tmp_path):
    """Con un WAV mono de 24 kHz (como el que guarda la aplicación) el filtro en una pasada se bloqueaba."""
    import importlib.util
    import wave as _wave
    spec = importlib.util.spec_from_file_location("cbw", ROOT / "voice_clone" / "chatterbox_worker.py")
    cbw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cbw)
    src = tmp_path / "mi_voz.wav"
    t = np.arange(24000 * 12) / 24000
    pcm = (0.45 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with _wave.open(str(src), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm.tobytes())
    dest = tmp_path / "_preparada_mi_voz.wav"
    dur = cbw.prepare_reference(src, dest)
    assert 11 < dur <= 12.1
    assert not list(tmp_path.glob("*_paso*.wav"))
