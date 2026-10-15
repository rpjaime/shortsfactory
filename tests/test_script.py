import json

import pytest

from image_processor import scan_folder
from project_manager import ProjectManager
from script_manager import (LLMError, ScriptError, _check_local_url, export_script_json, extract_json_object,
                            import_script_json, import_script_srt, parse_draft, parse_srt,
                            validate_project_script, validate_script_json)

EXAMPLE = {
    "schema_version": 1,
    "title": "5 herramientas de GitHub que debes conocer",
    "language": "es-ES",
    "voice": "default",
    "target_duration_seconds": 40,
    "slides": [
        {"image": "1-portada.png", "role": "hook", "narration": "Cinco herramientas de GitHub que merece la pena conocer."},
        {"image": "2-repo.jpg", "role": "content", "narration": "Esta herramienta permite automatizar tareas de desarrollo."},
    ],
    "outro": {"narration": "Guarda este vídeo y sígueme para descubrir más herramientas."},
}


def test_example_script_is_valid():
    assert validate_script_json(EXAMPLE, ["1-portada.png", "2-repo.jpg"]).ok


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d.update(schema_version=7), "no soportado"),
    (lambda d: d.update(slides=[]), "al menos una"),
    (lambda d: d["slides"][0].update(image="../../etc/passwd"), "sin rutas"),
    (lambda d: d["slides"][0].update(image="C:\\x.png"), "sin rutas"),
    (lambda d: d["slides"][0].update(role="villano"), "rol"),
    (lambda d: d["slides"][0].update(narration=5), "texto"),
    (lambda d: d.update(target_duration_seconds=999), "entre 5 y 180"),
    (lambda d: d["slides"][1].update(image="no-existe.png"), "no está en la carpeta"),
])
def test_invalid_scripts(mutate, expected):
    d = json.loads(json.dumps(EXAMPLE))
    mutate(d)
    res = validate_script_json(d, ["1-portada.png", "2-repo.jpg"])
    assert not res.ok and any(expected in e for e in res.errors), res.errors


def test_import_export_roundtrip(settings, image_folder, tmp_path):
    p = ProjectManager().create("Guion")
    names = scan_folder(image_folder)
    p.set_source_dir(image_folder, names)
    f = tmp_path / "guion.json"
    f.write_text(json.dumps(EXAMPLE, ensure_ascii=False), encoding="utf-8")
    res = import_script_json(p, f, names)
    assert res.ok
    assert [s["image"] for s in p.enabled_slides()] == ["1-portada.png", "2-repo.jpg"]
    assert p.data["outro"]["narration"].startswith("Guarda")
    assert p.data["timing"]["target_duration"] == 40
    out = export_script_json(p)
    assert validate_script_json(out, names).ok
    assert out["slides"][0]["narration"] == EXAMPLE["slides"][0]["narration"]


def test_import_invalid_json_reports_position(settings, image_folder, tmp_path):
    p = ProjectManager().create("Malo")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    f = tmp_path / "malo.json"
    f.write_text('{"schema_version": 1, "slides": [', encoding="utf-8")
    with pytest.raises(ScriptError, match="línea"):
        import_script_json(p, f, scan_folder(image_folder))


def test_srt_parse_and_import(settings, image_folder, tmp_path):
    srt = "1\n00:00:00,000 --> 00:00:02,500\nPrimera frase.\n\n2\n00:00:02,600 --> 00:00:05,000\nSegunda\nfrase.\n"
    cues = parse_srt(srt)
    assert cues[1]["text"] == "Segunda frase." and cues[0]["end"] == 2.5
    p = ProjectManager().create("SRT")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    f = tmp_path / "g.srt"
    f.write_text(srt, encoding="utf-8")
    res = import_script_srt(p, f)
    assert p.data["slides"][0]["narration"] == "Primera frase."
    assert any("ignoran" in w for w in res.warnings)
    with pytest.raises(ScriptError):
        parse_srt("1\nsin tiempo\ntexto\n")


def test_project_validation_requires_narration(settings, image_folder):
    p = ProjectManager().create("Val")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    res = validate_project_script(p)
    assert not res.ok and len(res.errors) == 5
    for s in p.data["slides"]:
        s["narration"] = "Texto correcto."
    p.data["slides"][4]["role"] = "silent"
    p.data["slides"][4]["narration"] = ""
    assert validate_project_script(p).ok


def test_lmstudio_url_must_be_local():
    assert _check_local_url("http://localhost:1234/v1/") == "http://localhost:1234/v1"
    assert _check_local_url("http://192.168.1.20:1234/v1")
    with pytest.raises(LLMError):
        _check_local_url("https://api.openai.com/v1")
    with pytest.raises(LLMError):
        _check_local_url("ftp://localhost")


def test_lmstudio_server_off_gives_clear_error(settings, image_folder):
    from script_manager import lmstudio_list_models
    with pytest.raises(LLMError, match="No se puede conectar"):
        lmstudio_list_models("http://127.0.0.1:9", timeout=2)


def test_parse_llm_output():
    raw = '<think>pienso</think>```json\n{"hook": "Atento.", "slides": [{"index": 1, "narration": "Uno."},' \
          ' {"index": 2, "narration": "", "needs_more_info": true}], "outro": "Sígueme."}\n```'
    obj = extract_json_object(raw)
    draft = parse_draft(obj, 2)
    assert draft["slides"][0] == "Atento. Uno."
    assert draft["outro"] == "Sígueme."
    assert any("falta información" in w for w in draft["warnings"])
    with pytest.raises(LLMError):
        extract_json_object("sin json")
