from project_manager import default_design
from subtitle_engine import build_cues, chunk_sentence, wrap_lines, write_ass, write_srt
from timeline import build_timeline

BASE = dict(fps=30, transition="fade", transition_duration=0.5, min_slide_duration=2.0, cover_duration=2.5,
            lead_margin=0.15, pause_between=0.4, silent_duration=2.5)


def make_tl():
    entries = [
        {"kind": "slide", "slide_id": "a", "image": "a.png", "audio_path": "a.wav", "audio_duration": 3.0,
         "segments": [{"text": "¿Conoces esta herramienta?", "start": 0.0, "end": 1.2},
                      {"text": "Automatiza tareas aburridas, ahorra tiempo y se integra con GitHub Actions.",
                       "start": 1.45, "end": 3.0}]},
        {"kind": "slide", "slide_id": "b", "image": "b.png", "audio_path": "b.wav", "audio_duration": 2.0,
         "segments": [{"text": "Segunda diapositiva.", "start": 0.0, "end": 2.0}]},
    ]
    return build_timeline(entries, **BASE)


def test_wrap_respects_max_chars():
    lines = wrap_lines("Automatiza tareas aburridas y ahorra muchísimo tiempo", 26)
    assert all(len(l) <= 26 for l in lines)
    assert " ".join(lines) == "Automatiza tareas aburridas y ahorra muchísimo tiempo"


def test_chunking_prefers_natural_pauses():
    blocks = chunk_sentence("Automatiza tareas aburridas, ahorra tiempo y se integra con GitHub Actions.", 26, 2)
    assert len(blocks) >= 2
    assert blocks[0][-1].endswith(",")
    assert all(len(b) <= 2 and all(len(l) <= 26 for l in b) for b in blocks)


def test_cues_synced_to_real_audio_and_transitions():
    tl = make_tl()
    cues = build_cues(tl, 26, 2)
    a, b = tl.items
    first = [c for c in cues if c.item_index == 0]
    second = [c for c in cues if c.item_index == 1]
    assert first[0].start == round(a.narration_start, 3)
    assert abs(first[-1].end - (a.narration_start + 3.0)) < 1e-3
    # el bloque de la segunda diapositiva empieza tras su transición de entrada
    assert second[0].start >= b.start + b.transition_in
    assert abs(second[0].start - (b.narration_start + 0.0)) < 1e-3
    # sin solapamientos y en orden
    for x, y in zip(cues, cues[1:]):
        assert x.end <= y.start + 1e-6
    # la pausa real entre frases se respeta (hueco entre 1.2 y 1.45)
    gap_cue = [c for c in first if abs(c.start - (a.narration_start + 1.45)) < 1e-3]
    assert gap_cue


def test_ass_and_srt_output(tmp_path):
    tl = make_tl()
    cues = build_cues(tl, 26, 2)
    d = default_design()
    ass = write_ass(cues, tmp_path / "s.ass", d, 1080, 1920, "Segoe UI").read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "¿Conoces esta herramienta?" in ass
    assert "\\an5\\pos(540," in ass
    srt = write_srt(cues, tmp_path / "s.srt").read_text(encoding="utf-8-sig")
    assert srt.startswith("1\n00:00:00,150 --> ")
    assert "¿Conoces" in srt


def test_ass_escapes_braces(tmp_path):
    from subtitle_engine import Cue
    d = default_design()
    txt = write_ass([Cue(0, 1, ["{\\b1}hack"], 0)], tmp_path / "x.ass", d, 540, 960, "Arial").read_text("utf-8-sig")
    assert "{\\b1}" not in txt.split("[Events]")[1].split("}", 1)[1]


def test_never_more_lines_than_allowed():
    import random
    rng = random.Random(3)
    words = "herramientas GitHub automatiza documentación código repositorios rápido sencillo útil".split()
    for _ in range(200):
        sent = " ".join(rng.choice(words) for _ in range(rng.randint(1, 30))) + "."
        for b in chunk_sentence(sent, 26, 2):
            assert 1 <= len(b) <= 2 and all(len(l) <= 26 or " " not in l for l in b)
    blocks = chunk_sentence("¿Buscas herramientas de GitHub que te ahorren horas?", 26, 2)
    assert all(len(b) <= 2 for b in blocks) and min(len(" ".join(b)) for b in blocks) > 10
