import json

import pytest

from conftest import ToneTTS
from pronunciation import DEFAULT_LEXICON, apply_lexicon, load_lexicon, save_lexicon
from tts_engine import KokoroEngine, NarrationCache, TTSError, VoiceParams


def test_apply_lexicon_whole_words_and_case():
    lex = [("GitHub", "guítjab"), ("IA", "i a"), ("GitHub Actions", "guítjab ácshons"), ("Node.js", "nóud yei es")]
    assert apply_lexicon("Usa GitHub y github.", lex) == "Usa guítjab y guítjab."
    assert apply_lexicon("GitHub Actions automatiza", lex) == "guítjab ácshons automatiza"
    assert apply_lexicon("La IA ayuda en los medios", lex) == "La i a ayuda en los medios"   # «media» intacto
    assert apply_lexicon("Instala Node.js hoy", lex) == "Instala nóud yei es hoy"
    assert apply_lexicon("GitHubber", lex) == "GitHubber"                                  # sólo palabras completas


def test_lexicon_persistence(settings):
    assert load_lexicon() == DEFAULT_LEXICON
    save_lexicon([("Kubernetes", "cubernetis"), ("", "x"), ("kubernetes", "duplicado")])
    assert load_lexicon() == [("Kubernetes", "cubernetis")]
    data = json.loads((settings.assets_path / "pronunciaciones.json").read_text(encoding="utf-8"))
    assert data["entradas"][0]["termino"] == "Kubernetes"


def test_subtitles_keep_original_text_and_cache_tracks_lexicon(settings, tmp_path):
    eng = ToneTTS()
    cache = NarrationCache()
    p = VoiceParams("tono")
    a = cache.synthesize(eng, "Mira GitHub. Es útil.", p, [("GitHub", "guítjab")])
    assert a.segments[0].text == "Mira GitHub."                 # el subtítulo no cambia
    b = cache.synthesize(eng, "Mira GitHub. Es útil.", p, [("GitHub", "guítjab")])
    assert b.cached
    c = cache.synthesize(eng, "Mira GitHub. Es útil.", p, [("GitHub", "guit jab")])
    assert not c.cached                                          # otra pronunciación → otro audio


def test_kokoro_errors_are_clear(tmp_path):
    eng = KokoroEngine(tmp_path)
    assert eng.list_voices() == []
    with pytest.raises(TTSError):
        eng.check_voice("ef_dora")          # modelo no descargado (o paquete no instalado)
    with pytest.raises(TTSError):
        eng.check_voice("xx_nadie")
