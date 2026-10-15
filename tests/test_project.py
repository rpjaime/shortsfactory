import json

import pytest

from image_processor import scan_folder
from project_manager import (SCHEMA_VERSION, PathSecurityError, ProjectError, ProjectManager, migrate,
                             safe_join)


def test_create_save_reopen(settings, image_folder):
    pm = ProjectManager()
    p = pm.create("Mi primer Short")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    p.data["slides"][0]["narration"] = "Hola, esto es una prueba."
    p.data["design"]["accent_color"] = "#ff0000"
    p.data["voice"]["speed"] = 1.1
    p.save()
    q = pm.open(p.id)
    assert q.data["slides"][0]["narration"] == "Hola, esto es una prueba."
    assert q.data["design"]["accent_color"] == "#ff0000"
    assert q.data["voice"]["speed"] == 1.1
    assert q.data["schema_version"] == SCHEMA_VERSION
    assert [s["image"] for s in q.data["slides"]] == scan_folder(image_folder)
    # Las imágenes originales no se copian
    assert not list(q.dir.rglob("*.png"))


def test_unique_ids_and_listing(settings):
    pm = ProjectManager()
    a = pm.create("Vídeo GitHub")
    b = pm.create("Vídeo GitHub")
    assert a.id != b.id and a.id == "video-github"
    ids = [x["id"] for x in pm.list_projects()]
    assert set(ids) == {a.id, b.id}


def test_recovery_from_backup(settings):
    pm = ProjectManager()
    p = pm.create("Recuperable")
    p.data["title"] = "Versión buena"
    p.save()
    p.data["title"] = "Versión 2"
    p.save()          # crea .bak con «Versión buena»
    p.manifest_path.write_text("{ roto", encoding="utf-8")
    q = pm.open(p.id)
    assert q.data["title"] == "Versión buena"


def test_open_missing_project_errors(settings):
    with pytest.raises(ProjectError):
        ProjectManager().open("no-existe")


def test_migration_from_v0():
    data = {"name": "viejo", "slides": ["a.png", {"image": "b.png", "narration": "x", "role": "raro"}]}
    m = migrate(data)
    assert m["schema_version"] == 1
    assert m["slides"][0]["image"] == "a.png" and m["slides"][0]["enabled"]
    assert m["slides"][1]["role"] == "content"
    assert "design" in m and "voice" in m


def test_migration_rejects_future_version():
    with pytest.raises(ProjectError):
        migrate({"schema_version": 99})


@pytest.mark.parametrize("bad", ["../secreto.png", "..\\secreto.png", "C:\\Windows\\win.ini", "/etc/passwd",
                                 "sub/../../x.png", "", "\\\\server\\share\\a.png"])
def test_safe_join_rejects_escape(tmp_path, bad):
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, bad)


def test_safe_join_accepts_names(tmp_path):
    assert safe_join(tmp_path, "a.png") == (tmp_path / "a.png").resolve()


def test_reorder_and_keep_text(settings, image_folder):
    p = ProjectManager().create("Orden")
    names = scan_folder(image_folder)
    p.set_source_dir(image_folder, names)
    p.data["slides"][2]["narration"] = "tercera"
    p.move_slide(2, -1)
    assert p.data["slides"][1]["narration"] == "tercera"
    p.move_slide_to(1, 4)
    assert p.data["slides"][4]["narration"] == "tercera"
    p.set_order([4, 0, 1, 2, 3])
    assert p.data["slides"][0]["narration"] == "tercera"
    with pytest.raises(ProjectError):
        p.set_order([0, 0, 1, 2, 3])
    # Re-escanear la carpeta conserva los textos
    p.set_source_dir(image_folder, names)
    assert any(s["narration"] == "tercera" for s in p.data["slides"])


def test_manifest_is_valid_json_after_save(settings):
    p = ProjectManager().create("JSON")
    json.loads(p.manifest_path.read_text(encoding="utf-8"))


def test_telemetry_disabled():
    import os
    import config  # noqa: F401
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert os.environ["GRADIO_ANALYTICS_ENABLED"] == "False"
