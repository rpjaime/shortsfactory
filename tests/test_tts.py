import json

import numpy as np
import pytest

from conftest import ToneTTS
from tts_engine import (NarrationCache, PiperEngine, TTSError, VoiceParams, read_wav_duration, split_sentences,
                        trim_silence)


def test_split_sentences_spanish():
    t = "¿Conoces esta herramienta? Sirve para todo, p. ej. tests. Cuesta 3.5 euros… ¡Pruébala! Fin"
    assert split_sentences(t) == ["¿Conoces esta herramienta?", "Sirve para todo, p. ej. tests.",
                                  "Cuesta 3.5 euros…", "¡Pruébala!", "Fin"]
    assert split_sentences("   ") == []


def test_trim_silence():
    sr = 1000
    a = np.concatenate([np.zeros(300), np.ones(100) * 0.5, np.zeros(500)]).astype(np.float32)
    out = trim_silence(a, sr, keep=0.01)
    assert 100 <= out.size <= 125


def test_synthesis_uses_real_durations(settings, tmp_path):
    eng = ToneTTS()
    p = VoiceParams("tono", sentence_pause=0.3)
    res = eng.synthesize("Primera frase corta. Segunda frase bastante más larga que la primera.", p,
                         tmp_path / "a.wav")
    assert len(res.segments) == 2
    s1, s2 = res.segments
    assert s1.start == 0 and s2.start > s1.end
    assert abs((s2.start - s1.end) - 0.3) < 0.01            # pausa configurada
    assert (s2.end - s2.start) > (s1.end - s1.start)        # duraciones distintas según el texto
    assert abs(read_wav_duration(res.wav_path) - res.duration) < 0.001
    assert abs(res.duration - s2.end) < 0.01                 # silencio final recortado


def test_cache_reuse_and_invalidation(settings):
    eng = ToneTTS()
    cache = NarrationCache()
    p = VoiceParams("tono")
    a = cache.synthesize(eng, "Hola mundo.", p)
    calls = eng.calls
    b = cache.synthesize(eng, "Hola mundo.", p)
    assert b.cached and eng.calls == calls and b.wav_path == a.wav_path
    c = cache.synthesize(eng, "Hola mundo.", VoiceParams("tono", speed=1.1))
    assert not c.cached and eng.calls > calls
    # Archivo dañado → se regenera
    with open(a.wav_path, "r+b") as fh:
        fh.truncate(100)
    d = cache.synthesize(eng, "Hola mundo.", p)
    assert not d.cached


def test_engine_errors_are_clear(settings, tmp_path):
    eng = ToneTTS(fail_on="error")
    with pytest.raises(TTSError):
        eng.synthesize("Esto provoca un error.", VoiceParams("tono"), tmp_path / "x.wav")
    with pytest.raises(TTSError, match="no está instalada"):
        eng.synthesize("Hola.", VoiceParams("otra"), tmp_path / "x.wav")
    with pytest.raises(TTSError):
        eng.synthesize("   ", VoiceParams("tono"), tmp_path / "x.wav")


# --- Piper: tratamiento de errores sin descargar modelos reales -------------

def test_piper_missing_voice(tmp_path):
    eng = PiperEngine(tmp_path)
    ok, _ = PiperEngine.is_available()
    with pytest.raises(TTSError) as exc:
        eng.check_voice("es_ES-davefx-medium")
    assert ("no está instalada" in str(exc.value)) or (not ok and "Piper" in str(exc.value))


def test_piper_rejects_bad_voice_id(tmp_path):
    with pytest.raises(TTSError):
        PiperEngine(tmp_path)._paths("../../malo")


def test_piper_incomplete_and_corrupt_models(tmp_path):
    ok, _ = PiperEngine.is_available()
    if not ok:
        pytest.skip("piper-tts no instalado")
    eng = PiperEngine(tmp_path)
    (tmp_path / "es_ES-x-medium.onnx").write_bytes(b"0" * 1000)
    (tmp_path / "es_ES-x-medium.onnx.json").write_text("{}", encoding="utf-8")
    with pytest.raises(TTSError, match="incompleta"):
        eng.check_voice("es_ES-x-medium")
    (tmp_path / "es_ES-x-medium.onnx").write_bytes(b"0" * 1_200_000)
    with pytest.raises(TTSError, match="no es una configuración"):
        eng.check_voice("es_ES-x-medium")
    cfg = {"audio": {"sample_rate": 22050}, "phoneme_id_map": {"a": [1]}, "num_speakers": 1,
           "espeak": {"voice": "es"}}
    (tmp_path / "es_ES-x-medium.onnx.json").write_text(json.dumps(cfg), encoding="utf-8")
    eng.check_voice("es_ES-x-medium")    # la estructura es válida…
    with pytest.raises(TTSError, match="incompatible o está dañado"):
        eng.synthesize_sentence("Hola.", VoiceParams("es_ES-x-medium"))   # …pero el modelo no
    voices = eng.list_voices()
    assert voices and voices[0]["ok"]
    (tmp_path / "es_ES-x-medium.onnx.json").write_text("{roto", encoding="utf-8")
    assert not eng.list_voices()[0]["ok"]
