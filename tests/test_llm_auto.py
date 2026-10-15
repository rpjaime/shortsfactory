"""Guion automático con un servidor falso que imita la API de LM Studio (sin red externa)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from image_processor import scan_folder
from project_manager import ProjectManager
from script_manager import AUTO_NOTES_PREFIX, LLMError, auto_script, choose_models, encode_image_for_llm


class FakeLMStudio(BaseHTTPRequestHandler):
    calls: list = []
    fail_images: list = []      # True = la siguiente petición con imagen falla
    reject_schema = False
    models = [{"id": "qwen-texto", "type": "llm", "state": "loaded"},
              {"id": "gemma-vision", "type": "vlm", "state": "not-loaded"},
              {"id": "nomic-embed", "type": "embeddings", "state": "not-loaded"}]

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/v0/models":
            self._send({"data": self.models})
        else:
            self._send({"data": [{"id": m["id"]} for m in self.models]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if FakeLMStudio.reject_schema and "response_format" in req:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "response_format not supported"}')
            return
        FakeLMStudio.calls.append(req)
        content = req["messages"][-1]["content"]
        if isinstance(content, list):   # petición con imagen
            assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            n = len([c for c in FakeLMStudio.calls if isinstance(c["messages"][-1]["content"], list)])
            ans = {"recurso": f"repo-{n}", "texto_visible": f"owner/repo-{n} Automatiza tareas",
                   "descripcion": "Captura de un repositorio de GitHub.", "es_portada": n == 1, "legible": True}
            text = "```json\n" + json.dumps(ans) + "\n```"
        else:
            assert "/no_think" in content
            assert "repo-" in content or "Información" in content
            n = content.count("Imagen «")
            ans = {"hook": "¿Conoces estas herramientas?",
                   "slides": [{"index": i, "narration": f"Narración generada {i}."} for i in range(1, n + 1)],
                   "outro": "Sígueme para más."}
            text = "<think></think>" + json.dumps(ans, ensure_ascii=False)
        if FakeLMStudio.fail_images and isinstance(content, list):
            name_hit = FakeLMStudio.fail_images.pop(0)
            if name_hit:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "modelo saturado"}')
                return
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(text), 7):
                chunk = {"choices": [{"delta": {"content": text[i:i + 7]}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
            return
        self._send({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


@pytest.fixture()
def lmstudio():
    FakeLMStudio.calls = []
    FakeLMStudio.fail_images = []
    FakeLMStudio.reject_schema = False
    srv = HTTPServer(("127.0.0.1", 0), FakeLMStudio)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_choose_models_prefers_vision(lmstudio):
    txt, vlm, notes = choose_models(lmstudio, "", "", 5, True)
    assert vlm == "gemma-vision" and txt == "gemma-vision"     # mismo modelo: no carga dos a la vez
    assert any("cargará" in n for n in notes)
    with pytest.raises(LLMError, match="no admite imágenes"):
        choose_models(lmstudio, "", "qwen-texto", 5, True)
    txt, vlm, _ = choose_models(lmstudio, "", "", 5, False)
    assert vlm is None and txt == "qwen-texto"


def test_auto_script_reads_images_and_fills_table(settings, image_folder, lmstudio):
    pm = ProjectManager()
    p = pm.create("Auto")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    p.data["slides"][2]["narration"] = "Texto mío que no debe cambiar."
    p.data["slides"][3]["notes"] = "Nota escrita por mí."
    res = auto_script(p, lmstudio, "", "", 30, read_images=True, overwrite=False)
    slides = p.data["slides"]
    assert res["read"] == 4                                   # la nota manual evita leer esa imagen
    assert slides[0]["narration"].startswith("¿Conoces estas herramientas?")
    assert slides[2]["narration"] == "Texto mío que no debe cambiar."
    assert slides[3]["notes"] == "Nota escrita por mí."
    assert slides[1]["notes"].startswith(AUTO_NOTES_PREFIX) and "repo-" in slides[1]["notes"]
    assert p.data["outro"]["narration"] == "Sígueme para más."
    assert res["written"] == 4
    # Segunda ejecución: las lecturas se reutilizan; con sobrescribir, se reescribe todo
    calls_before = len(FakeLMStudio.calls)
    res2 = auto_script(pm.open(p.id), lmstudio, "", "", 30, read_images=True, overwrite=True)
    assert res2["read"] == 0 and res2["reused"] == 4
    assert len(FakeLMStudio.calls) == calls_before + 1        # sólo la petición del guion
    assert res2["written"] == 5


def test_encode_image_is_small_jpeg(image_folder):
    uri = encode_image_for_llm(image_folder / "1-portada.png", max_side=512)
    assert uri.startswith("data:image/jpeg;base64,") and len(uri) < 200_000


def test_failed_image_does_not_lose_previous_work(settings, image_folder, lmstudio):
    pm = ProjectManager()
    p = pm.create("Fallos")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    # 3.ª imagen falla dos veces (intento + reintento); el resto va bien
    FakeLMStudio.fail_images = [False, False, True, True]
    res = auto_script(p, lmstudio, "", "", 30, read_images=True)
    assert res["failed"] == [3] and res["read"] == 4
    assert any("no se pudo leer" in w for w in res["warnings"])
    q = pm.open(p.id)                                     # lo leído está guardado en disco
    assert sum(1 for s in q.data["slides"] if s.get("vision")) == 4
    # Reintento: sólo se lee la imagen que faltaba
    res2 = auto_script(q, lmstudio, "", "", 30, read_images=True)
    assert res2["read"] == 1 and res2["reused"] == 4 and res2["failed"] == []


def test_structured_output_and_fallback(settings, image_folder, lmstudio):
    p = ProjectManager().create("Esquema")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    auto_script(p, lmstudio, "", "", 30, read_images=True)
    assert all("response_format" in c for c in FakeLMStudio.calls)     # JSON forzado por esquema
    FakeLMStudio.calls.clear()
    FakeLMStudio.reject_schema = True                                  # servidor antiguo: sin esquema
    q = ProjectManager().create("Sin esquema")
    q.set_source_dir(image_folder, scan_folder(image_folder))
    res = auto_script(q, lmstudio, "", "", 30, read_images=True)
    assert res["read"] == 5 and not any("response_format" in c for c in FakeLMStudio.calls)


def test_plain_text_answer_is_salvaged(monkeypatch, image_folder):
    import script_manager as sm
    monkeypatch.setattr(sm, "structured_completion",
                        lambda *a, **k: ("La imagen muestra la portada de un repositorio llamado demo-tool.", "stop"))
    r = sm.read_slide_image("http://127.0.0.1:1/v1", "m", 5, image_folder / "1-portada.png")
    assert "demo-tool" in r["descripcion"]
