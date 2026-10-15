"""ShortsFactory — punto de entrada e interfaz web local (Gradio).

Ejecuta ``python app.py`` (o ``start.bat``). La interfaz sólo escucha en 127.0.0.1.
"""
from __future__ import annotations

import os

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"   # nada de telemetría (ver también config.py)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

import functools
import html
import json
import logging
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"gradio(\..*)?")
warnings.filterwarnings("ignore", message=r".*show_api.*")

import gradio as gr
import pandas as pd

import config
from config import (APP_NAME, APP_VERSION, FONT_EXTENSIONS, KNOWN_SPANISH_VOICES, MUSIC_EXTENSIONS,
                    X264_PRESETS, Settings, get_settings, load_settings, save_settings, setup_logging)
from fonts import list_user_fonts, resolve_font_path
from image_processor import analyze_folder, hex_to_rgb, make_thumbnail, scan_folder
from jobs import JOBS, JobBusy
from project_manager import SLIDE_ROLES, Project, ProjectError, ProjectManager, default_design
from quality_checks import inspect_ffmpeg
from script_manager import (LLMError, ScriptError, auto_script, export_script_json, import_script_json,
                            import_script_srt, lmstudio_list_models, normalize_text, slides_missing_info,
                            validate_project_script)
from thumbnail import STYLES as THUMB_STYLES
from thumbnail import ThumbnailError, default_source, default_title, default_thumbnail, make_cover, save_jpeg
from tiktok_import import TikTokImportError, update_gallery_dl
from tiktok_import import import_post as import_tiktok_post
from templates import COMPOSITIONS, MOTIONS, TEMPLATES, TRANSITIONS, template_choices, template_values
from pronunciation import DEFAULT_LEXICON, load_lexicon, save_lexicon
from tts_engine import (ENGINE_LABELS, KokoroEngine, NarrationCache, PiperEngine, TTSError, VoiceParams, all_voices,
                        download_kokoro, download_voice, get_engine)
from video_renderer import (RenderError, clear_cache, make_timeline, narration_items, preview_mix, render_project, render_still,
                            synthesize_narration)

log = logging.getLogger("shortsfactory")


# ---------------------------------------------------------------------------
# Utilidades de la interfaz
# ---------------------------------------------------------------------------

class UserError(Exception):
    """Error que se muestra tal cual al usuario."""


def safe(fn):
    """Ningún error cierra la aplicación: se registra y se muestra un mensaje comprensible."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except gr.Error:
            raise
        except (UserError, ProjectError, ScriptError, TTSError, RenderError, LLMError, FileNotFoundError,
                ValueError, JobBusy) as exc:
            log.warning("%s: %s", fn.__name__, exc)
            # Error previsto y explicado al usuario: sin volcar la traza en la consola
            raise gr.Error(str(exc), duration=None, print_exception=False) from exc
        except Exception as exc:  # noqa: BLE001
            log.error("Error inesperado en %s:\n%s", fn.__name__, traceback.format_exc())
            raise gr.Error(f"Error inesperado ({exc.__class__.__name__}: {exc}). Detalles en logs/shortsfactory.log",
                           duration=None) from exc
    return wrapper


def pm() -> ProjectManager:
    return ProjectManager(get_settings().projects_path)


def need_project(pid: str | None) -> Project:
    if not pid:
        raise UserError("Primero crea o abre un proyecto en la pestaña «Proyectos».")
    return pm().open(pid)


def to_hex(color: str, default: str = "#000000") -> str:
    r, g, b = hex_to_rgb(color or default, hex_to_rgb(default))
    return f"#{r:02x}{g:02x}{b:02x}"


def open_folder(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606 - abre el Explorador en una carpeta local
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return f"Carpeta abierta: `{path}`"
    except Exception as exc:  # noqa: BLE001
        return f"No se pudo abrir la carpeta automáticamente ({exc}). Ruta: `{path}`"


def pick_folder_dialog(initial: str = "") -> str | None:
    """Selector de carpeta nativo (tkinter). Devuelve None si no está disponible."""
    result: dict[str, Any] = {}

    def run():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["path"] = filedialog.askdirectory(initialdir=initial or None, title="Carpeta del carrusel")
            root.destroy()
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=600)
    if "error" in result:
        log.warning("Selector de carpetas no disponible: %s", result["error"])
        return None
    return result.get("path") or ""


# ---------------------------------------------------------------------------
# Proyectos
# ---------------------------------------------------------------------------

def project_choices() -> list[tuple[str, str]]:
    return [(f"{p['name']}  ·  {p['updated_at'].replace('T', ' ')}", p["id"]) for p in pm().list_projects()]


def project_summary(p: Project | None) -> str:
    if p is None:
        return "Ningún proyecto abierto."
    n = len(p.data["slides"])
    act = len(p.enabled_slides())
    with_text = sum(1 for s in p.enabled_slides() if s.get("narration", "").strip())
    last = p.data.get("last_export") or {}
    lines = [f"### 📁 {html.escape(p.name)}",
             f"- Carpeta de imágenes: `{p.data.get('source_dir') or '— sin asignar —'}`",
             f"- Diapositivas: {act} activas de {n} · {with_text} con narración",
             f"- Plantilla: {TEMPLATES.get(p.data['design'].get('template'), {}).get('label', '?')} · "
             f"Voz: `{p.data['voice'].get('voice_id')}` · Duración objetivo: {p.data['timing'].get('target_duration')} s",
             f"- Última modificación: {p.data.get('updated_at', '').replace('T', ' ')}",
             f"- Directorio del proyecto: `{p.dir}`"]
    for kind, rep in last.items():
        lines.append(f"- Última exportación ({kind}): `{rep.get('output')}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Construcción de la interfaz
# ---------------------------------------------------------------------------

CSS = """
.sf-progress {width:100%;background:#2a2f3a;border-radius:8px;height:22px;overflow:hidden;margin:6px 0}
.sf-progress > div {height:100%;background:#2f81f7;color:#fff;font-size:12px;line-height:22px;padding-left:8px;
  white-space:nowrap}
.sf-indet {height:22px;line-height:22px;font-size:13px;opacity:.85}
footer {display:none !important}
/* El sondeo de progreso (cada segundo) no debe atenuar los resultados */
.gradio-container .pending {opacity:1 !important}
"""

ROLE_HELP = "hook = gancho · content = contenido · transition = transición · cta = llamada a la acción · silent = sin voz"


def build_ui() -> gr.Blocks:
    s = get_settings()
    theme = gr.themes.Soft(primary_hue="blue", font=["Segoe UI", "system-ui", "sans-serif"],
                           font_mono=["Consolas", "monospace"])
    with gr.Blocks(title=APP_NAME, theme=theme, css=CSS, analytics_enabled=False) as demo:
        pid = gr.State("")
        sel_idx = gr.State(0)
        gr.Markdown(f"# 🎬 {APP_NAME} <small>v{APP_VERSION}</small>\nConvierte carruseles en YouTube Shorts "
                    "con voz local, subtítulos y música. Todo se procesa en este equipo.")
        header = gr.Markdown("Ningún proyecto abierto.")

        with gr.Tabs():
            # ============================ PROYECTOS ============================
            with gr.Tab("1 · Proyectos"):
                with gr.Row():
                    with gr.Column():
                        new_name = gr.Textbox(label="Nombre del nuevo proyecto", placeholder="5 herramientas de GitHub")
                        btn_create = gr.Button("➕ Crear proyecto", variant="primary")
                    with gr.Column():
                        proj_dd = gr.Dropdown(label="Proyectos existentes", choices=project_choices(), value=None)
                        with gr.Row():
                            btn_open = gr.Button("📂 Abrir", variant="primary")
                            btn_refresh_proj = gr.Button("🔄 Actualizar lista")
                with gr.Accordion("Reutilizar configuración de otro proyecto (segundo vídeo)", open=False):
                    gr.Markdown("Copia diseño, voz, audio, tiempos y exportación desde otro proyecto al abierto. "
                                "Las narraciones ya generadas se reutilizan desde la caché si el texto y la voz coinciden.")
                    copy_from = gr.Dropdown(label="Copiar desde", choices=project_choices())
                    btn_copy = gr.Button("Copiar configuración")
                proj_info = gr.Markdown()

            # ============================ IMÁGENES ============================
            with gr.Tab("2 · Imágenes"):
                with gr.Row():
                    folder_tb = gr.Textbox(label="Carpeta con las imágenes del carrusel (PNG, JPG, JPEG, WEBP)",
                                           placeholder=r"D:\Carruseles\github-5-herramientas", scale=5)
                    btn_browse = gr.Button("📁 Examinar…", scale=1)
                    btn_load = gr.Button("Cargar / volver a escanear", variant="primary", scale=1)
                with gr.Accordion("📲 Importar desde TikTok (URL de un carrusel de fotos)", open=False):
                    gr.Markdown("Pega el enlace de una publicación de fotos (p. ej. `https://www.tiktok.com/@usuario/photo/…` "
                                "o `https://vm.tiktok.com/…`). Se descargan sólo las imágenes, en su orden, a la carpeta "
                                "del proyecto, y el texto de la publicación se usa como contexto para el guion con IA. "
                                "**Úsalo con tus publicaciones o con contenido que tengas permiso para reutilizar.**")
                    with gr.Row():
                        tiktok_url = gr.Textbox(label="URL de TikTok", scale=5,
                                                placeholder="https://www.tiktok.com/@usuario/photo/1234567890")
                        btn_tiktok = gr.Button("⬇ Importar carrusel", variant="primary", scale=1)
                    tiktok_cookies = gr.Textbox(label="Archivo de cookies (opcional, sólo para publicaciones privadas)",
                                                placeholder=r"C:\Users\tu_usuario\Downloads\cookies-tiktok.txt")
                    tiktok_msg = gr.Markdown()
                gallery = gr.Gallery(label="Diapositivas (haz clic para seleccionar)", columns=6, height=420,
                                     allow_preview=False, object_fit="contain")
                with gr.Row():
                    sel_md = gr.Markdown("Selecciona una diapositiva.")
                with gr.Row():
                    btn_up = gr.Button("⬆ Subir")
                    btn_down = gr.Button("⬇ Bajar")
                    pos_num = gr.Number(label="Mover a la posición", precision=0, minimum=1, value=1, scale=1)
                    btn_move = gr.Button("Mover")
                    btn_toggle = gr.Button("⛔ Excluir / ✅ Incluir")
                img_table = gr.Dataframe(label="Análisis de imágenes", interactive=False, wrap=True,
                                         headers=["#", "Archivo", "Activa", "Dimensiones", "Relación", "Avisos"])
                with gr.Accordion("Ajustes de la diapositiva seleccionada (composición, escala, posición, movimiento)",
                                  open=False):
                    with gr.Row():
                        ov_comp = gr.Dropdown(label="Composición", choices=[("Usar la global", "")] +
                                              [(v, k) for k, v in COMPOSITIONS.items()], value="")
                        ov_force = gr.Checkbox(label="Forzar «llenar» aunque recorte mucho", value=False)
                        ov_motion = gr.Dropdown(label="Movimiento", choices=[("Usar el global", "")] +
                                                [(v, k) for k, v in MOTIONS.items()], value="")
                    with gr.Row():
                        ov_scale = gr.Slider(0.6, 1.5, value=1.0, step=0.01, label="Escala")
                        ov_offx = gr.Slider(-0.3, 0.3, value=0.0, step=0.005, label="Desplazamiento horizontal")
                        ov_offy = gr.Slider(-0.3, 0.3, value=0.0, step=0.005, label="Desplazamiento vertical")
                    with gr.Row():
                        ov_fx = gr.Slider(0, 1, value=0.5, step=0.01, label="Zona visible X (modo llenar)")
                        ov_fy = gr.Slider(0, 1, value=0.5, step=0.01, label="Zona visible Y (modo llenar)")
                    with gr.Row():
                        btn_ov_apply = gr.Button("Aplicar y previsualizar", variant="primary")
                        btn_ov_reset = gr.Button("Restablecer")
                    slide_preview = gr.Image(label="Fotograma compuesto (540×960)", height=520, interactive=False)

            # ============================ GUION ============================
            with gr.Tab("3 · Guion"):
                gr.Markdown(f"Edita el texto de cada diapositiva. Roles: {ROLE_HELP}. Las columnas «#» e «Imagen» "
                            "no se editan aquí (el orden se cambia en «Imágenes»).")
                script_df = gr.Dataframe(
                    headers=["#", "Imagen", "Rol", "Titular", "Narración", "Notas para la IA"],
                    datatype=["number", "str", "str", "str", "str", "str"], interactive=True, wrap=True,
                    col_count=(6, "fixed"), static_columns=[0, 1], column_widths=["4%", "14%", "9%", "15%", "38%", "20%"],
                    label="Guion por diapositiva (sólo activas)")
                with gr.Row():
                    outro_on = gr.Checkbox(label="Incluir cierre (outro)", value=True)
                    outro_text = gr.Textbox(label="Narración del cierre", lines=2,
                                            placeholder="Guarda este vídeo y sígueme para descubrir más herramientas.")
                    outro_card = gr.Textbox(label="Texto de la tarjeta de cierre", lines=2,
                                            placeholder="¿Te ha servido? Guárdalo y sígueme")
                with gr.Row():
                    btn_save_script = gr.Button("💾 Guardar guion", variant="primary")
                    btn_validate = gr.Button("✔ Validar")
                script_msg = gr.Markdown()
                with gr.Accordion("Importar / exportar (JSON o SRT)", open=False):
                    with gr.Row():
                        import_file = gr.File(label="Archivo de guion (.json o .srt)", file_types=[".json", ".srt"])
                        with gr.Column():
                            btn_import = gr.Button("Importar")
                            btn_export_json = gr.Button("Exportar JSON")
                            btn_export_srt = gr.Button("Exportar SRT del último render")
                        export_file = gr.File(label="Archivo exportado", interactive=False)
                with gr.Accordion("🤖 Guion automático con IA local (LM Studio)", open=True):
                    gr.Markdown("La IA **lee cada imagen** (texto visible y contenido) y escribe el guion completo: gancho, "
                                "una narración por diapositiva y cierre. El resultado aparece en la tabla de arriba, donde "
                                "puedes editarlo. Sólo usa lo que se ve en las imágenes y lo que escribas en «Notas para "
                                "la IA» (tus notas tienen prioridad sobre la lectura automática). Todo ocurre en tu equipo.")
                    with gr.Row():
                        llm_topic = gr.Textbox(label="Tema general (opcional)",
                                               placeholder="Herramientas de GitHub para desarrolladores")
                        llm_extra = gr.Textbox(label="Indicaciones adicionales (opcional)",
                                               placeholder="Tono cercano, frases cortas, tuteando")
                    with gr.Row():
                        llm_read = gr.Checkbox(label="Leer el contenido de las imágenes (modelo con visión)", value=True)
                        llm_overwrite = gr.Checkbox(label="Sobrescribir las narraciones ya escritas", value=False)
                    btn_llm = gr.Button("🤖 Generar guion automáticamente", variant="primary")
                    llm_msg = gr.Markdown()

            # ============================ VOZ ============================
            with gr.Tab("4 · Voz") as tab_voz:
                with gr.Row():
                    voice_dd = gr.Dropdown(label="Voz instalada (Piper o Kokoro)", choices=[], value=None, scale=3)
                    speaker_dd = gr.Dropdown(label="Locutor (voces con varios)", choices=[], value=None, scale=1)
                    btn_voices_refresh = gr.Button("🔄", scale=0, min_width=60)
                with gr.Row():
                    v_speed = gr.Slider(0.85, 1.15, value=1.0, step=0.01, label="Velocidad (1,0 = natural)")
                    v_pause = gr.Slider(0.0, 1.0, value=0.25, step=0.05, label="Pausa entre frases (s)")
                voice_status = gr.Markdown()
                with gr.Row():
                    test_text = gr.Textbox(label="Frase de prueba", scale=4,
                                           value="Hoy te traigo cinco herramientas de GitHub que te van a ahorrar horas. "
                                                 "¿Cuál vas a probar primero?")
                    btn_test_voice = gr.Button("▶ Probar esta voz", scale=1)
                test_audio = gr.Audio(label="Prueba de voz", type="filepath", interactive=False)
                with gr.Accordion("🎙 Voz clonada: grabaciones de referencia", open=False):
                    gr.Markdown("Graba o sube 15-30 s de tu voz (sitio silencioso, tono natural de vídeo). Cada grabación "
                                "aparece como una voz «Voz clonada · nombre». Usa sólo tu voz o la de alguien que te haya "
                                "dado permiso. El audio generado lleva una marca de agua inaudible (PerTh, Resemble AI).\n\n"
                                "_Texto sugerido:_ «Hola, ¿qué tal? Hoy te traigo cinco herramientas gratuitas que me han "
                                "ahorrado muchísimas horas de trabajo. La primera es sencilla, pero increíblemente útil: "
                                "organiza tus proyectos en un par de clics. ¿Y lo mejor? Funciona en tu propio ordenador, "
                                "sin pagar nada. Quédate hasta el final, porque la última te va a sorprender. ¡Vamos allá!»")
                    with gr.Row():
                        ref_audio = gr.Audio(label="Grabación", sources=["microphone", "upload"], type="filepath")
                        with gr.Column():
                            ref_name = gr.Textbox(label="Nombre de la voz", value="mi_voz")
                            btn_ref_save = gr.Button("💾 Guardar como voz de referencia", variant="primary")
                            ref_msg = gr.Markdown()
                with gr.Accordion("Pronunciaciones (términos técnicos que la voz lee mal)", open=False):
                    gr.Markdown("La voz lee el texto de la columna «Pronunciación» en lugar del término; los subtítulos "
                                "siguen mostrando el término original. Sirve para Piper y Kokoro. Escribe la pronunciación "
                                "como se diría en español, con tilde en la sílaba fuerte (p. ej. GitHub → guítjab).")
                    lex_df = gr.Dataframe(headers=["Término", "Pronunciación"], datatype=["str", "str"],
                                          interactive=True, col_count=(2, "fixed"), wrap=True,
                                          label="Diccionario de pronunciaciones")
                    with gr.Row():
                        btn_lex_save = gr.Button("💾 Guardar pronunciaciones", variant="primary")
                        btn_lex_reset = gr.Button("Restablecer las predeterminadas")
                    lex_msg = gr.Markdown()
                with gr.Accordion("Descargar voces españolas (acción explícita, fuente oficial verificada)", open=False):
                    dl_voice = gr.Dropdown(label="Voz", choices=[(f"Piper · {k}", k) for k in KNOWN_SPANISH_VOICES]
                                           + [("Kokoro-82M · Dora, Alex y Santa (≈354 MB)", "kokoro")],
                                           value=config.DEFAULT_VOICE_ID)
                    dl_info = gr.Markdown()
                    btn_dl = gr.Button("⬇ Descargar y verificar")
                    dl_log = gr.Markdown()
                with gr.Row():
                    btn_tts = gr.Button("🗣 Generar narración", variant="primary")
                tts_table = gr.Dataframe(headers=["#", "Imagen", "Estado", "Duración (s)", "Texto"], interactive=False,
                                         wrap=True, label="Fragmentos de narración (texto actual del guion; "
                                                          "pulsa «Generar narración» para los pendientes)")
                with gr.Row():
                    frag_dd = gr.Dropdown(label="Escuchar fragmento", choices=[], value=None)
                    frag_audio = gr.Audio(label="Fragmento", type="filepath", interactive=False)

            # ============================ DISEÑO ============================
            with gr.Tab("5 · Diseño"):
                with gr.Row():
                    tpl_dd = gr.Dropdown(label="Plantilla visual", choices=template_choices(), value="tech_dark")
                    btn_tpl = gr.Button("Aplicar valores de la plantilla")
                tpl_desc = gr.Markdown()
                with gr.Row():
                    c_bg = gr.ColorPicker(label="Fondo")
                    c_accent = gr.ColorPicker(label="Acento")
                    c_text = gr.ColorPicker(label="Texto")
                    c_sub = gr.ColorPicker(label="Subtítulos")
                with gr.Row():
                    font_dd = gr.Dropdown(label="Tipografía", choices=[("Fuente del sistema", "")], value="")
                    font_up = gr.File(label="Añadir fuente (.ttf/.otf)", file_types=[".ttf", ".otf"])
                    bold_cb = gr.Checkbox(label="Negrita", value=True)
                with gr.Row():
                    title_size = gr.Slider(36, 110, step=1, label="Tamaño de titulares")
                    sub_size = gr.Slider(36, 100, step=1, label="Tamaño de subtítulos")
                    sub_pos = gr.Slider(0.15, 0.5, step=0.01, label="Posición vertical subtítulos (desde abajo)")
                with gr.Row():
                    sub_chars = gr.Slider(14, 40, step=1, label="Caracteres por línea")
                    sub_lines = gr.Slider(1, 3, step=1, label="Líneas máximas")
                    sub_box = gr.Checkbox(label="Fondo semitransparente tras el subtítulo")
                    sub_box_op = gr.Slider(0.2, 0.9, step=0.05, label="Opacidad del fondo")
                with gr.Row():
                    show_head = gr.Checkbox(label="Mostrar titulares de diapositiva")
                    head_style = gr.Dropdown(label="Estilo de titular", choices=[("Sencillo", "plain"),
                                             ("Barra de acento", "bar"), ("Resaltado", "highlight")])
                    brand_tb = gr.Textbox(label="Marca o nombre de canal (opcional)")
                with gr.Row():
                    comp_dd = gr.Dropdown(label="Composición global", choices=[(v, k) for k, v in COMPOSITIONS.items()])
                    motion_dd = gr.Dropdown(label="Movimiento", choices=[(v, k) for k, v in MOTIONS.items()])
                    intensity = gr.Slider(0, 1, step=0.05, label="Intensidad del movimiento")
                    seed_nb = gr.Number(label="Semilla", precision=0)
                with gr.Row():
                    trans_dd = gr.Dropdown(label="Transición", choices=[(v, k) for k, v in TRANSITIONS.items()])
                    trans_dur = gr.Slider(0.1, 1.0, step=0.05, label="Duración de transición (s)")
                    vfade = gr.Slider(0.0, 1.0, step=0.05, label="Fundido de entrada/salida del vídeo (s)")
                with gr.Row():
                    min_dur = gr.Slider(1.0, 6.0, step=0.1, label="Duración mínima por diapositiva (s)")
                    cover_dur = gr.Slider(1.0, 6.0, step=0.1, label="Duración mínima de la portada (s)")
                    outro_mode = gr.Dropdown(label="Cierre", choices=[("Tarjeta de cierre", "card"),
                                             ("Sobre la última diapositiva", "last_slide")])
                gr.Markdown("**Ritmo y duración**")
                with gr.Row():
                    target_dur = gr.Slider(10, 180, step=1, label="Duración objetivo (s)")
                    lead = gr.Slider(0.0, 0.6, step=0.05, label="Margen antes de cada frase (s)")
                    pause_b = gr.Slider(0.1, 1.5, step=0.05, label="Pausa tras cada narración (s)")
                    fill_cb = gr.Checkbox(label="Alargar diapositivas hasta la duración objetivo")
                design_msg = gr.Markdown()
                with gr.Row():
                    style_text = gr.Textbox(label="Texto de prueba", value="¿Sabías que esta herramienta te ahorra horas? ¡Pruébala!")
                    style_slide = gr.Number(label="Diapositiva", value=1, precision=0, minimum=1)
                    btn_style = gr.Button("👁 Previsualizar estilo", variant="primary")
                style_img = gr.Image(label="Previsualización del estilo (subtítulo renderizado con libass)", height=560,
                                     interactive=False)

            # ============================ AUDIO ============================
            with gr.Tab("6 · Audio"):
                gr.Markdown("⚠️ No se incluye música. Usa pistas propias o con licencia que permita el uso previsto. "
                            "Verifica también la licencia de la voz antes de publicar contenido comercial.")
                with gr.Row():
                    music_dd = gr.Dropdown(label="Música de fondo", choices=[("Sin música", "")], value="")
                    btn_music_refresh = gr.Button("🔄", scale=0, min_width=60)
                    music_up = gr.File(label="Añadir pista a assets/music", file_types=list(MUSIC_EXTENSIONS))
                with gr.Row():
                    m_vol = gr.Slider(0.0, 1.0, step=0.01, label="Volumen de la música")
                    v_vol = gr.Slider(0.3, 1.5, step=0.05, label="Volumen de la voz")
                    duck_cb = gr.Checkbox(label="Bajar la música cuando habla la voz")
                    duck_str = gr.Slider(0.0, 1.0, step=0.05, label="Intensidad de la atenuación")
                with gr.Row():
                    fin = gr.Slider(0.0, 5.0, step=0.1, label="Fundido de entrada de la música (s)")
                    fout = gr.Slider(0.0, 5.0, step=0.1, label="Fundido de salida de la música (s)")
                audio_msg = gr.Markdown()
                btn_mix = gr.Button("🎧 Escuchar mezcla de prueba")
                mix_audio = gr.Audio(label="Mezcla", type="filepath", interactive=False)

            # ============================ EXPORTACIÓN ============================
            with gr.Tab("7 · Exportación") as tab_export:
                with gr.Row():
                    crf = gr.Slider(14, 30, step=1, label="Calidad CRF (menor = más calidad)")
                    preset = gr.Dropdown(label="Preajuste de codificación", choices=list(X264_PRESETS))
                    fps_dd = gr.Dropdown(label="Fotogramas por segundo", choices=[24, 25, 30, 60])
                gr.Markdown("Formato: MP4 · H.264 yuv420p · AAC · 1080×1920 (9:16) · faststart.")
                with gr.Row():
                    btn_prev = gr.Button("⚡ Previsualización rápida (540×960)")
                    btn_final = gr.Button("🎬 Renderizar vídeo final (1080×1920)", variant="primary")
                    btn_cancel = gr.Button("⏹ Cancelar", variant="stop")
                with gr.Accordion("🖼 Miniatura (portada del Short)", open=False):
                    gr.Markdown("Imagen 1080×1920 en JPG (< 2 MB) para subirla como miniatura en YouTube Studio. "
                                "YouTube la recorta a una franja central (≈3:2) en la página principal y en tu canal: "
                                "el titular siempre queda dentro de esa franja (líneas amarillas en la vista previa). "
                                "Escribe \\*entre asteriscos\\* las palabras que quieras resaltar con el color de acento.")
                    with gr.Row():
                        with gr.Column(scale=3):
                            th_img = gr.Dropdown(label="Imagen de fondo", choices=[], value=None)
                            th_title = gr.Textbox(label="Titular", lines=2,
                                                  placeholder="5 herramientas de *GitHub* que te ahorran horas")
                            th_sub = gr.Textbox(label="Etiqueta (opcional)", placeholder="Gratis y sin conexión")
                            th_style = gr.Radio(label="Estilo", choices=[(v, k) for k, v in THUMB_STYLES.items()],
                                                value="banda")
                            th_auto = gr.Checkbox(label="Crear la miniatura automáticamente con cada vídeo final",
                                                  value=True)
                            with gr.Row():
                                btn_th_prev = gr.Button("👁 Vista previa")
                                btn_th_save = gr.Button("💾 Guardar miniatura", variant="primary")
                            th_msg = gr.Markdown()
                        th_preview = gr.Image(label="Miniatura", type="filepath", interactive=False, height=560,
                                              scale=2)
                job_html = gr.HTML("<div class='sf-indet'>Sin trabajos en curso.</div>")
                job_log = gr.Textbox(label="Registro", lines=10, max_lines=18, interactive=False, autoscroll=True)
                with gr.Row():
                    result_video = gr.Video(label="Resultado", height=640, interactive=False)
                    with gr.Column():
                        result_md = gr.Markdown()
                        btn_open_out = gr.Button("📂 Abrir carpeta de salida")
                        open_msg = gr.Markdown()
                timeline_df = gr.Dataframe(label="Línea de tiempo del último render", interactive=False)
                timer = gr.Timer(1.0)
                last_shown = gr.State("")

            # ============================ AJUSTES ============================
            with gr.Tab("8 · Ajustes"):
                with gr.Row():
                    ff_path = gr.Textbox(label="Ruta de ffmpeg (vacío = buscar en el PATH)", value=s.ffmpeg_path)
                    fp_path = gr.Textbox(label="Ruta de ffprobe (vacío = junto a ffmpeg o en el PATH)", value=s.ffprobe_path)
                    btn_ff = gr.Button("Comprobar FFmpeg")
                ff_msg = gr.Markdown()
                with gr.Row():
                    lm_url = gr.Textbox(label="URL de LM Studio", value=s.lmstudio_url)
                    lm_model = gr.Textbox(label="Modelo de texto (vacío = automático)", value=s.lmstudio_model)
                    lm_vmodel = gr.Textbox(label="Modelo con visión (vacío = automático)", value=s.lmstudio_vision_model)
                    lm_to = gr.Number(label="Tiempo máx. sin respuesta (s)", value=s.lmstudio_timeout)
                    btn_lm = gr.Button("Comprobar conexión")
                lm_msg = gr.Markdown()
                gr.Markdown("**Carpetas** (vacío = predeterminada dentro de la aplicación)")
                with gr.Row():
                    d_proj = gr.Textbox(label="Proyectos", value=s.projects_dir, placeholder=str(s.projects_path))
                    d_out = gr.Textbox(label="Salida", value=s.outputs_dir, placeholder=str(s.outputs_path))
                    d_tmp = gr.Textbox(label="Temporales", value=s.temp_dir, placeholder=str(s.temp_path))
                with gr.Row():
                    d_cache = gr.Textbox(label="Caché", value=s.cache_dir, placeholder=str(s.cache_path))
                    d_assets = gr.Textbox(label="Recursos (fuentes, música)", value=s.assets_dir, placeholder=str(s.assets_path))
                    d_voices = gr.Textbox(label="Voces de Piper", value=s.voices_dir, placeholder=str(s.voices_path))
                with gr.Row():
                    btn_save_settings = gr.Button("💾 Guardar ajustes", variant="primary")
                    btn_clear_cache = gr.Button("🧹 Vaciar caché (audio y clips)")
                    btn_open_logs = gr.Button("📂 Abrir registros")
                    btn_gdl_update = gr.Button("⬆ Actualizar gallery-dl (TikTok)")
                settings_msg = gr.Markdown()
                env_md = gr.Markdown()
                btn_env = gr.Button("Diagnóstico del entorno")

        # =================================================================
        # Lógica
        # =================================================================

        # ---------- carga del proyecto en todos los controles ----------
        def gallery_items(p: Project):
            items = []
            src = p.source_dir
            for i, sl in enumerate(p.data["slides"], 1):
                try:
                    path = p.image_path(sl["image"])
                    thumb = make_thumbnail(path, p.cache_dir / "thumbs" / (Path(sl["image"]).stem + ".jpg"))
                    cap = f"{i} · {sl['image']}" + ("" if sl.get("enabled", True) else "  ⛔ excluida")
                    items.append((str(thumb), cap))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Miniatura no disponible para %s: %s", sl["image"], exc)
            return items

        def image_table(p: Project):
            if not p.source_dir or not p.source_dir.is_dir():
                return pd.DataFrame(columns=["#", "Archivo", "Activa", "Dimensiones", "Relación", "Avisos"])
            names = [s["image"] for s in p.data["slides"]]
            infos = {i.name: i for i in analyze_folder(p.source_dir, names)}
            rows = []
            for k, sl in enumerate(p.data["slides"], 1):
                inf = infos.get(sl["image"])
                if inf is None:
                    continue
                warn = "; ".join(([inf.error] if inf.error else []) + inf.warnings
                                 + (["Orientación EXIF corregida"] if inf.exif_rotated else [])
                                 + (["Con transparencia"] if inf.has_alpha else []))
                rows.append([k, sl["image"], "Sí" if sl.get("enabled", True) else "No",
                             f"{inf.width}×{inf.height}" if inf.ok else "-", inf.aspect_label, warn])
            return pd.DataFrame(rows, columns=["#", "Archivo", "Activa", "Dimensiones", "Relación", "Avisos"])

        def script_frame(p: Project):
            rows = [[i, s["image"], s.get("role", "content"), s.get("headline", ""), s.get("narration", ""),
                     s.get("notes", "")] for i, s in enumerate(p.enabled_slides(), 1)]
            return pd.DataFrame(rows, columns=["#", "Imagen", "Rol", "Titular", "Narración", "Notas para la IA"])

        def voice_key(engine: str, vid: str) -> str:
            return f"{engine}|{vid}"

        def voice_choices():
            return [((v["label"] if v["engine"] == "kokoro" else f"Piper · {v['label']}"),
                     voice_key(v["engine"], v["id"])) for v in all_voices()]

        def project_voice_key(p: Project) -> str:
            v = p.data["voice"]
            return voice_key(v.get("engine", "piper"), v.get("voice_id", ""))

        def speaker_choices(p: Project):
            key = project_voice_key(p)
            for v in all_voices():
                if voice_key(v["engine"], v["id"]) == key and v["speakers"]:
                    return [(f"{name} ({idx})", str(idx)) for name, idx in v["speakers"]]
            return []

        def lexicon_frame():
            return pd.DataFrame(load_lexicon(), columns=["Término", "Pronunciación"])

        def font_choices():
            return [("Fuente del sistema", "")] + [(f, f) for f in list_user_fonts()]

        def music_choices():
            d = get_settings().music_path
            files = sorted(f.name for f in d.iterdir() if f.suffix.lower() in MUSIC_EXTENSIONS) if d.is_dir() else []
            return [("Sin música", "")] + [(f, f) for f in files]

        def fragments_view(p: Project):
            """Texto ACTUAL del guion de cada fragmento y si su audio está generado o pendiente."""
            cache = {c["slide_id"]: c for c in (p.data.get("narration_cache") or [])}
            v = p.data.get("voice", {})
            voice_key = f"{v.get('engine', 'piper')}|{v.get('voice_id', '')}"
            rows, choices = [], []
            for i, it in enumerate(narration_items(p), 1):
                name = "(cierre)" if it["slide_id"] == "outro" else it["image"]
                if not it["text"]:
                    rows.append([i, name, "— sin voz", "", ""])
                    continue
                c = cache.get(it["slide_id"])
                fresh = (c is not None and c.get("text") == it["text"] and c.get("audio_path")
                         and Path(c["audio_path"]).is_file() and c.get("voice", voice_key) == voice_key)
                if fresh:
                    rows.append([i, name, "✅ generado", round(c.get("duration") or 0, 2), it["text"]])
                    choices.append((f"{i} · {name} ({c['duration']:.2f} s)", c["audio_path"]))
                else:
                    rows.append([i, name, "⏳ pendiente" if c is None else "✏️ texto o voz cambiados", "",
                                 it["text"]])
            return pd.DataFrame(rows, columns=["#", "Imagen", "Estado", "Duración (s)", "Texto"]), choices

        def design_values(p: Project):
            d, t, a, e = p.data["design"], p.data["timing"], p.data["audio"], p.data["export"]
            return [
                d.get("template"), TEMPLATES.get(d.get("template"), {}).get("description", ""),
                to_hex(d.get("bg_color")), to_hex(d.get("accent_color")), to_hex(d.get("text_color")),
                to_hex(d.get("subtitle_color")), gr.update(choices=font_choices(), value=d.get("font_file", "")),
                d.get("font_weight_bold", True), d.get("title_size"), d.get("subtitle_size"),
                d.get("subtitle_position"), d.get("subtitle_max_chars"), d.get("subtitle_max_lines"),
                d.get("subtitle_box"), d.get("subtitle_box_opacity"), d.get("show_headlines"), d.get("headline_style"),
                d.get("brand_text", ""), d.get("composition"), d.get("motion"), d.get("motion_intensity"),
                d.get("seed"), d.get("transition"), d.get("transition_duration"), d.get("video_fade"),
                d.get("min_slide_duration"), d.get("cover_duration"), d.get("outro_mode"),
                t.get("target_duration"), t.get("lead_margin"), t.get("pause_between"), t.get("fill_to_target"),
            ]

        def audio_values(p: Project):
            a = p.data["audio"]
            return [gr.update(choices=music_choices(), value=a.get("music_file", "")), a.get("music_volume"),
                    a.get("voice_volume"), a.get("ducking"), a.get("ducking_strength"), a.get("fade_in"), a.get("fade_out")]

        def export_values(p: Project):
            e = p.data["export"]
            return [e.get("crf"), e.get("preset"), e.get("fps")]

        def voice_values(p: Project):
            v = p.data["voice"]
            vc = voice_choices()
            ids = [c[1] for c in vc]
            key = project_voice_key(p)
            val = key if key in ids else (ids[0] if ids else None)
            sc = speaker_choices(p)
            sid = v.get("speaker_id")
            status = voice_status_md(p, ids)
            return [gr.update(choices=vc, value=val), gr.update(choices=sc, value=str(sid) if sid is not None and sc else None),
                    v.get("speed", 1.0), v.get("sentence_pause", 0.25), status]

        def voice_status_md(p: Project, ids: list[str]) -> str:
            engine = p.data["voice"].get("engine", "piper")
            avail = get_engine(engine).is_available()
            if not avail[0]:
                return f"❌ {avail[1]}"
            if not ids:
                return ("⚠️ No hay ninguna voz instalada. Descarga una en el apartado de abajo. "
                        f"Carpeta de voces: `{get_settings().voices_path}`")
            key = project_voice_key(p)
            if key not in ids:
                return f"⚠️ La voz del proyecto (`{key}`) no está instalada. Elige otra o descárgala."
            extra = (" La velocidad no se aplica a la voz clonada (habla a su ritmo natural). La primera frase de "
                     "cada sesión tarda ~30 s más mientras se carga el modelo; después, ~4 s de cálculo por segundo "
                     "de voz (se guarda en caché)." if engine == "chatterbox" else "")
            return f"✅ Voz {ENGINE_LABELS.get(engine, engine)} `{p.data['voice'].get('voice_id')}` lista.{extra}"

        ALL_PROJECT_OUTPUTS = [header, proj_info, folder_tb, gallery, img_table, script_df, outro_on, outro_text,
                               outro_card, voice_dd, speaker_dd, v_speed, v_pause, voice_status, tts_table, frag_dd,
                               tpl_dd, tpl_desc, c_bg, c_accent, c_text, c_sub, font_dd, bold_cb, title_size, sub_size,
                               sub_pos, sub_chars, sub_lines, sub_box, sub_box_op, show_head, head_style, brand_tb,
                               comp_dd, motion_dd, intensity, seed_nb, trans_dd, trans_dur, vfade, min_dur, cover_dur,
                               outro_mode, target_dur, lead, pause_b, fill_cb, music_dd, m_vol, v_vol, duck_cb,
                               duck_str, fin, fout, crf, preset, fps_dd, proj_dd, copy_from]

        def load_all(p: Project):
            frag_rows, frag_ch = fragments_view(p)
            o = p.data["outro"]
            choices = project_choices()
            return ([f"**Proyecto abierto:** {html.escape(p.name)} · guardado automático activado", project_summary(p),
                     p.data.get("source_dir", ""), gallery_items(p), image_table(p), script_frame(p),
                     o.get("enabled", True), o.get("narration", ""), o.get("text", "")]
                    + voice_values(p) + [frag_rows, gr.update(choices=frag_ch, value=None)]
                    + design_values(p) + audio_values(p) + export_values(p)
                    + [gr.update(choices=choices, value=p.id), gr.update(choices=choices)])

        @safe
        def on_create(name):
            p = pm().create(name)
            return [p.id, 0] + load_all(p)

        @safe
        def on_open(project_id):
            if not project_id:
                raise UserError("Elige un proyecto de la lista.")
            p = pm().open(project_id)
            return [p.id, 0] + load_all(p)

        btn_create.click(on_create, [new_name], [pid, sel_idx] + ALL_PROJECT_OUTPUTS)
        btn_open.click(on_open, [proj_dd], [pid, sel_idx] + ALL_PROJECT_OUTPUTS)
        btn_refresh_proj.click(lambda: [gr.update(choices=project_choices()), gr.update(choices=project_choices())],
                               None, [proj_dd, copy_from])

        @safe
        def on_copy(current, source):
            p = need_project(current)
            if not source or source == current:
                raise UserError("Elige otro proyecto del que copiar la configuración.")
            pm().duplicate_settings_into(pm().open(source), p)
            p = pm().open(current)
            gr.Info("Configuración copiada.")
            return [p.id, 0] + load_all(p)

        btn_copy.click(on_copy, [pid, copy_from], [pid, sel_idx] + ALL_PROJECT_OUTPUTS)

        # ---------- imágenes ----------
        @safe
        def on_browse(current_folder):
            path = pick_folder_dialog(current_folder)
            if path is None:
                raise UserError("El selector de carpetas no está disponible; pega la ruta en el cuadro de texto.")
            return path or current_folder

        btn_browse.click(on_browse, [folder_tb], [folder_tb])

        @safe
        def on_load_images(project_id, folder):
            p = need_project(project_id)
            return load_folder_into(p, folder)

        def load_folder_into(p: Project, folder):
            folder = ("" if folder is None else str(folder)).strip().strip('"')
            if not folder:
                raise UserError("Indica la carpeta que contiene las imágenes.")
            names = scan_folder(folder)
            if not names:
                raise UserError("La carpeta no contiene imágenes PNG, JPG, JPEG ni WEBP.")
            p.set_source_dir(folder, names)
            infos = analyze_folder(folder, names)
            for inf in infos:
                if not inf.ok:
                    sl = next(s for s in p.data["slides"] if s["image"] == inf.name)
                    sl["enabled"] = False
            p.save()
            bad = [i.name for i in infos if not i.ok]
            dup = [w for i in infos for w in i.warnings if "duplicado" in w]
            msg = f"{len(names)} imágenes cargadas y ordenadas de forma natural."
            if bad:
                msg += f" Ilegibles (excluidas): {', '.join(bad)}."
            if dup:
                msg += f" {len(dup)} posible(s) duplicado(s): revisa la tabla."
            gr.Info(msg)
            return [gallery_items(p), image_table(p), script_frame(p), project_summary(p), 0,
                    selection_md(p, 0)]

        def selection_md(p: Project, idx: int) -> str:
            if not p.data["slides"]:
                return "Sin diapositivas."
            idx = max(0, min(idx, len(p.data["slides"]) - 1))
            sl = p.data["slides"][idx]
            ov = sl.get("overrides") or {}
            return (f"**Seleccionada: {idx + 1} · {sl['image']}** — {'activa' if sl.get('enabled', True) else 'excluida'}"
                    + (f" · ajustes propios: `{json.dumps(ov, ensure_ascii=False)}`" if ov else ""))

        btn_load.click(on_load_images, [pid, folder_tb], [gallery, img_table, script_df, proj_info, sel_idx, sel_md])

        @safe
        def on_tiktok(project_id, url, cookies, topic, progress=gr.Progress()):
            p = need_project(project_id)
            progress(0.1, desc="Descargando el carrusel de TikTok…")
            try:
                post = import_tiktok_post(url, p.dir, (cookies or "").strip().strip('"') or None)
            except TikTokImportError as exc:
                raise UserError(str(exc)) from exc
            progress(0.8, desc="Cargando las imágenes…")
            p.data["tiktok"] = {"url": post.url, "id": post.post_id, "author": post.author,
                                "description": post.description}
            outs = load_folder_into(p, post.folder)
            msg = [f"✅ {len(post.images)} imágenes importadas de TikTok"
                   + (f" (@{post.author})" if post.author else "") + f" en `{post.folder}`."]
            if post.description:
                msg.append(f"Texto de la publicación: _{html.escape(post.description[:300])}_")
            msg += [f"- ⚠️ {w}" for w in post.warnings]
            new_topic = topic
            if post.description and not (topic or "").strip():
                new_topic = re.sub(r"\s*#\S+", "", post.description).strip()[:200] or topic
            return outs + [str(post.folder), "\n\n".join(msg), new_topic]

        btn_tiktok.click(on_tiktok, [pid, tiktok_url, tiktok_cookies, llm_topic],
                         [gallery, img_table, script_df, proj_info, sel_idx, sel_md, folder_tb, tiktok_msg, llm_topic],
                         concurrency_limit=1)

        @safe
        def on_select(project_id, evt: gr.SelectData):
            p = need_project(project_id)
            idx = int(evt.index if not isinstance(evt.index, (list, tuple)) else evt.index[0])
            sl = p.data["slides"][idx]
            ov = sl.get("overrides") or {}
            return [idx, selection_md(p, idx), idx + 1, ov.get("composition", ""), bool(ov.get("force_fill", False)),
                    ov.get("motion", ""), ov.get("scale", 1.0), ov.get("offset_x", 0.0), ov.get("offset_y", 0.0),
                    ov.get("focus_x", 0.5), ov.get("focus_y", 0.5)]

        gallery.select(on_select, [pid], [sel_idx, sel_md, pos_num, ov_comp, ov_force, ov_motion, ov_scale, ov_offx,
                                          ov_offy, ov_fx, ov_fy])

        def _after_order_change(p: Project, idx: int):
            p.save()
            return [gr.update(value=gallery_items(p), selected_index=idx), image_table(p), script_frame(p), idx,
                    selection_md(p, idx), idx + 1]

        @safe
        def on_move(project_id, idx, delta):
            p = need_project(project_id)
            new = p.move_slide(int(idx), int(delta))
            return _after_order_change(p, new)

        @safe
        def on_move_to(project_id, idx, pos):
            p = need_project(project_id)
            new = p.move_slide_to(int(idx), int(pos or 1) - 1)
            return _after_order_change(p, new)

        @safe
        def on_toggle(project_id, idx):
            p = need_project(project_id)
            sl = p.data["slides"][int(idx)]
            sl["enabled"] = not sl.get("enabled", True)
            return _after_order_change(p, int(idx))

        order_outputs = [gallery, img_table, script_df, sel_idx, sel_md, pos_num]
        btn_up.click(lambda a, b: on_move(a, b, -1), [pid, sel_idx], order_outputs)
        btn_down.click(lambda a, b: on_move(a, b, 1), [pid, sel_idx], order_outputs)
        btn_move.click(on_move_to, [pid, sel_idx, pos_num], order_outputs)
        btn_toggle.click(on_toggle, [pid, sel_idx], order_outputs)

        @safe
        def on_apply_override(project_id, idx, comp, force, motion, scale, offx, offy, fx, fy):
            p = need_project(project_id)
            sl = p.data["slides"][int(idx)]
            ov = {}
            if comp:
                ov["composition"] = comp
            if force:
                ov["force_fill"] = True
            if motion:
                ov["motion"] = motion
            if abs(float(scale) - 1.0) > 1e-6:
                ov["scale"] = round(float(scale), 3)
            if abs(float(offx)) > 1e-6:
                ov["offset_x"] = round(float(offx), 4)
            if abs(float(offy)) > 1e-6:
                ov["offset_y"] = round(float(offy), 4)
            if comp == "fill" or abs(float(fx) - 0.5) > 1e-6 or abs(float(fy) - 0.5) > 1e-6:
                ov["focus_x"], ov["focus_y"] = round(float(fx), 3), round(float(fy), 3)
            sl["overrides"] = ov
            p.save()
            out = p.cache_dir / "previews" / f"slide_{sl['id']}.png"
            img, warns = render_still(p, int(idx), None, out)
            for w in warns:
                gr.Warning(w)
            return [str(img), selection_md(p, int(idx))]

        btn_ov_apply.click(on_apply_override, [pid, sel_idx, ov_comp, ov_force, ov_motion, ov_scale, ov_offx, ov_offy,
                                               ov_fx, ov_fy], [slide_preview, sel_md])

        @safe
        def on_reset_override(project_id, idx):
            p = need_project(project_id)
            p.data["slides"][int(idx)]["overrides"] = {}
            p.save()
            img, _ = render_still(p, int(idx), None, p.cache_dir / "previews" / "reset.png")
            return [str(img), selection_md(p, int(idx)), "", False, "", 1.0, 0.0, 0.0, 0.5, 0.5]

        btn_ov_reset.click(on_reset_override, [pid, sel_idx], [slide_preview, sel_md, ov_comp, ov_force, ov_motion,
                                                                ov_scale, ov_offx, ov_offy, ov_fx, ov_fy])

        # ---------- guion ----------
        def apply_script_frame(p: Project, df, on, otext, ocard):
            rows = df.values.tolist() if isinstance(df, pd.DataFrame) else (df or [])
            enabled = p.enabled_slides()
            if len(rows) != len(enabled):
                raise UserError("La tabla no coincide con las diapositivas activas; recarga el proyecto.")
            for sl, row in zip(enabled, rows):
                role = str(row[2] or "content").strip().lower()
                if role not in SLIDE_ROLES:
                    raise UserError(f"Rol no válido «{role}» en la diapositiva {row[0]}. Usa: {', '.join(SLIDE_ROLES)}.")
                sl["role"] = role
                sl["headline"] = normalize_text(str(row[3] or ""))
                sl["narration"] = normalize_text(str(row[4] or ""))
                sl["notes"] = str(row[5] or "").strip()
            p.data["outro"].update(enabled=bool(on), narration=normalize_text(otext or ""), text=(ocard or "").strip())

        @safe
        def on_save_script(project_id, df, on, otext, ocard):
            p = need_project(project_id)
            apply_script_frame(p, df, on, otext, ocard)
            p.save()
            res = validate_project_script(p)
            return [res.as_markdown(), project_summary(p)]

        btn_save_script.click(on_save_script, [pid, script_df, outro_on, outro_text, outro_card], [script_msg, proj_info])
        # Guardado automático al editar
        script_df.input(on_save_script, [pid, script_df, outro_on, outro_text, outro_card], [script_msg, proj_info],
                        show_progress="hidden")
        for comp in (outro_text, outro_card):
            comp.blur(on_save_script, [pid, script_df, outro_on, outro_text, outro_card], [script_msg, proj_info],
                      show_progress="hidden")
        outro_on.input(on_save_script, [pid, script_df, outro_on, outro_text, outro_card], [script_msg, proj_info],
                       show_progress="hidden")

        @safe
        def on_validate(project_id):
            p = need_project(project_id)
            return validate_project_script(p).as_markdown()

        btn_validate.click(on_validate, [pid], [script_msg])

        @safe
        def on_import(project_id, file):
            p = need_project(project_id)
            if not file:
                raise UserError("Selecciona un archivo .json o .srt.")
            path = Path(file if isinstance(file, str) else file.name)
            available = [s["image"] for s in p.data["slides"]]
            if path.suffix.lower() == ".json":
                res = import_script_json(p, path, available)
            elif path.suffix.lower() == ".srt":
                res = import_script_srt(p, path)
            else:
                raise UserError("Formato no admitido: usa .json o .srt.")
            if res.ok:
                p.save()
            o = p.data["outro"]
            return [res.as_markdown(), script_frame(p), o.get("enabled"), o.get("narration", ""), o.get("text", ""),
                    gallery_items(p), image_table(p)]

        btn_import.click(on_import, [pid, import_file], [script_msg, script_df, outro_on, outro_text, outro_card,
                                                          gallery, img_table])

        @safe
        def on_export_json(project_id):
            p = need_project(project_id)
            out = p.exports_dir / f"{p.id}_guion.json"
            out.write_text(json.dumps(export_script_json(p), ensure_ascii=False, indent=2), encoding="utf-8")
            return str(out)

        @safe
        def on_export_srt(project_id):
            p = need_project(project_id)
            for name in ("final.srt", "preview.srt"):
                f = p.subtitles_dir / name
                if f.is_file():
                    return str(f)
            raise UserError("Aún no hay subtítulos sincronizados: genera una previsualización o el vídeo final.")

        btn_export_json.click(on_export_json, [pid], [export_file])
        btn_export_srt.click(on_export_srt, [pid], [export_file])

        @safe
        def on_llm(project_id, df, on, otext, ocard, topic, extra, read_images, overwrite,
                   progress=gr.Progress()):
            p = need_project(project_id)
            apply_script_frame(p, df, on, otext, ocard)
            p.save()
            if not p.enabled_slides():
                raise UserError("Carga las imágenes primero (pestaña Imágenes).")
            if not read_images:
                missing = slides_missing_info(p)
                if missing:
                    raise UserError("Sin leer las imágenes, la IA necesita información en «Notas para la IA» "
                                    f"(faltan en las diapositivas {', '.join(map(str, missing))}).")
            st = get_settings()
            progress(0, desc="Conectando con LM Studio…")
            res = auto_script(p, st.lmstudio_url, st.lmstudio_model, st.lmstudio_vision_model, st.lmstudio_timeout,
                              read_images=bool(read_images), overwrite=bool(overwrite), topic=topic or "",
                              extra=extra or "", progress=lambda f, m: progress(f, desc=m))
            o = p.data["outro"]
            msg = [f"✅ Guion generado con `{res['text_model']}`"
                   + (f" (imágenes leídas con `{res['vision_model']}`)" if res["vision_model"] else "") + ".",
                   f"- Narraciones escritas: {res['written']}/{res['total']}"
                   + ("" if overwrite else " (las que ya tenían texto se han respetado)"),
                   f"- Imágenes leídas: {res['read']} · reutilizadas de una lectura anterior: {res['reused']}"
                   + (f" · **sin leer: {', '.join(map(str, res['failed']))}** (vuelve a pulsar para reintentarlas)"
                      if res.get("failed") else ""),
                   "- Revisa y edita el texto en la tabla; los cambios se guardan solos."]
            msg += [f"- ⚠️ {w}" for w in res["warnings"]]
            return [script_frame(p), o.get("enabled", True), o.get("narration", ""), "\n".join(msg),
                    validate_project_script(p).as_markdown()]

        btn_llm.click(on_llm, [pid, script_df, outro_on, outro_text, outro_card, llm_topic, llm_extra, llm_read,
                               llm_overwrite], [script_df, outro_on, outro_text, llm_msg, script_msg],
                      concurrency_limit=1)

        # ---------- voz ----------
        @safe
        def on_voice_change(project_id, voice_id, speaker, speed, pause):
            if not project_id:
                return gr.update()
            p = need_project(project_id)
            v = p.data["voice"]
            if voice_id and "|" in voice_id:
                engine, vid = voice_id.split("|", 1)
                if engine != v.get("engine") or vid != v.get("voice_id"):
                    speaker = None
                v["engine"], v["voice_id"] = engine, vid
            v["speaker_id"] = int(speaker) if speaker not in (None, "", "None") else None
            v["speed"] = float(speed)
            v["sentence_pause"] = float(pause)
            p.save()
            return voice_status_md(p, [c[1] for c in voice_choices()])

        for c in (voice_dd, speaker_dd, v_speed, v_pause):
            c.input(on_voice_change, [pid, voice_dd, speaker_dd, v_speed, v_pause], [voice_status],
                     show_progress="hidden")

        @safe
        def on_voices_refresh(project_id):
            if not project_id:
                vc = voice_choices()
                return [gr.update(choices=vc), gr.update(), "Abre un proyecto para elegir la voz."]
            p = need_project(project_id)
            vals = voice_values(p)
            return [vals[0], vals[1], vals[4]]

        btn_voices_refresh.click(on_voices_refresh, [pid], [voice_dd, speaker_dd, voice_status])

        def dl_info_md(vid):
            if vid == "kokoro":
                return ("**Kokoro-82M** (licencia Apache-2.0) — voces españolas Dora (femenina), Alex y Santa "
                        "(masculinas). Más natural que Piper; algo más lenta en CPU.\n\n"
                        f"- Origen: `{config.KOKORO_RELEASE_URL}` (publicación oficial de kokoro-onnx)\n"
                        "- Se verifican tamaño y SHA-256 de `kokoro-v1.0.onnx` (325 MB) y `voices-v1.0.bin` (28 MB).")
            info = KNOWN_SPANISH_VOICES.get(vid, {})
            return (f"**{vid}** — {info.get('description', '')}\n\n- Licencia de los datos: {info.get('dataset_license', '?')}\n"
                    f"- Ficha del modelo: {info.get('model_card', '')}\n- Origen: `{config.PIPER_VOICES_BASE_URL}` "
                    "(se verifica tamaño y MD5 con el índice oficial)\n\nVerifica la licencia antes de un uso comercial.")

        dl_voice.change(dl_info_md, [dl_voice], [dl_info])

        @safe
        def on_download(project_id, vid):
            lines = []
            if vid == "kokoro":
                download_kokoro(progress=lambda m: lines.append(m))
            else:
                download_voice(vid, progress=lambda m: lines.append(m))
            msg = "\n".join(f"- {l}" for l in lines) + "\n\n✅ Voz descargada y verificada."
            if project_id:
                p = need_project(project_id)
                if project_voice_key(p) not in [c[1] for c in voice_choices()] and vid != "kokoro":
                    p.data["voice"].update(engine="piper", voice_id=vid)
                    p.save()
                vals = voice_values(p)
                return [msg, vals[0], vals[1], vals[4]]
            return [msg, gr.update(choices=voice_choices()), gr.update(), ""]

        btn_dl.click(on_download, [pid, dl_voice], [dl_log, voice_dd, speaker_dd, voice_status], concurrency_limit=1)

        @safe
        def on_test_voice(voice_key_value, speaker, speed, pause, text):
            if not voice_key_value or "|" not in voice_key_value:
                raise UserError("Elige una voz instalada.")
            text = normalize_text(text or "")
            if not text:
                raise UserError("Escribe una frase de prueba.")
            engine, vid = voice_key_value.split("|", 1)
            params = VoiceParams(voice_id=vid, speed=float(speed), sentence_pause=float(pause),
                                 speaker_id=int(speaker) if speaker not in (None, "", "None") else None)
            res = NarrationCache().synthesize(get_engine(engine), text, params, load_lexicon())
            return res.wav_path

        @safe
        def on_ref_save(audio_path, name, project_id):
            if not audio_path:
                raise UserError("Graba o sube primero un audio.")
            name = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "mi_voz").strip()).strip("_") or "mi_voz"
            dest_dir = config.APP_DIR / "assets" / "voz_referencia"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{name}.wav"
            ff = config.ffmpeg_exe()
            if not ff:
                raise UserError("No se encuentra FFmpeg para convertir la grabación.")
            r = subprocess.run([ff, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(audio_path),
                                "-ac", "1", "-ar", "24000", str(dest)], capture_output=True, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                raise UserError(f"No se pudo guardar la grabación: {r.stderr[-300:]}")
            from tts_engine import read_wav_duration
            dur = read_wav_duration(dest)
            warn = " ⚠️ Es corta: graba al menos 15 s para que se parezca más a ti." if dur < 12 else ""
            vals = voice_values(need_project(project_id)) if project_id else [gr.update(choices=voice_choices())]
            return [f"✅ Guardada `{dest.name}` ({dur:.1f} s).{warn} Elígela arriba como «Voz clonada · {name}».",
                    vals[0]]

        btn_ref_save.click(on_ref_save, [ref_audio, ref_name, pid], [ref_msg, voice_dd])

        btn_test_voice.click(on_test_voice, [voice_dd, speaker_dd, v_speed, v_pause, test_text], [test_audio],
                             concurrency_limit=1)

        @safe
        def on_lex_save(df):
            rows = df.values.tolist() if isinstance(df, pd.DataFrame) else (df or [])
            path = save_lexicon([(str(r[0] or ""), str(r[1] or "")) for r in rows])
            return [lexicon_frame(), f"💾 {len(load_lexicon())} pronunciaciones guardadas en `{path}`. "
                                     "La caché de voz se regenera sólo para los textos afectados."]

        def on_lex_reset():
            save_lexicon(DEFAULT_LEXICON)
            return [lexicon_frame(), "Pronunciaciones predeterminadas restablecidas."]

        btn_lex_save.click(on_lex_save, [lex_df], [lex_df, lex_msg])
        btn_lex_reset.click(safe(on_lex_reset), None, [lex_df, lex_msg])

        @safe
        def on_tts(project_id, df, on, otext, ocard, progress=gr.Progress()):
            p = need_project(project_id)
            apply_script_frame(p, df, on, otext, ocard)
            res = validate_project_script(p)
            if not res.ok:
                raise UserError("Completa el guion antes de generar la voz:\n" + "\n".join(res.errors))
            if JOBS.current is not None and JOBS.current.running:
                raise UserError("Hay un renderizado en curso; espera a que termine.")
            class _Progress:
                cancel_event = threading.Event()

                def progress(self, frac, detail=""):
                    progress(frac if frac is not None else 0, desc=detail or "Generando narración…")

                def check_cancel(self):
                    pass

                def log(self, msg):
                    log.info(msg)

            progress(0, desc="Preparando la voz (la voz clonada tarda ~30 s en cargar la primera vez)…")
            synthesize_narration(p, _Progress())
            p.save()
            tl = make_timeline(p, [dict(slide_id=c["slide_id"], image="", kind="slide", audio_path=c["audio_path"],
                                        audio_duration=c["duration"], segments=c["segments"], overrides={})
                                   for c in p.data["narration_cache"]], int(p.data["export"].get("fps", 30)))
            rows, ch = fragments_view(p)
            msg = (f"✅ Narración lista: {sum(c['duration'] for c in p.data['narration_cache']):.1f} s de voz; "
                   f"duración estimada del vídeo {tl.total:.1f} s (objetivo {p.data['timing']['target_duration']:.0f} s).")
            for w in tl.warnings:
                msg += f"\n\n⚠️ {w}"
            return [rows, gr.update(choices=ch, value=ch[0][1] if ch else None), msg]

        def on_voz_tab(project_id):
            if not project_id:
                return [gr.update(), gr.update()]
            try:
                p = need_project(project_id)
            except Exception:  # noqa: BLE001
                return [gr.update(), gr.update()]
            rows, ch = fragments_view(p)
            return [rows, gr.update(choices=ch, value=ch[0][1] if ch else None)]

        tab_voz.select(on_voz_tab, [pid], [tts_table, frag_dd], show_progress="hidden")

        btn_tts.click(on_tts, [pid, script_df, outro_on, outro_text, outro_card], [tts_table, frag_dd, voice_status],
                      concurrency_limit=1)
        frag_dd.change(lambda f: f if f else None, [frag_dd], [frag_audio])

        # ---------- diseño ----------
        DESIGN_INPUTS = [tpl_dd, c_bg, c_accent, c_text, c_sub, font_dd, bold_cb, title_size, sub_size, sub_pos,
                         sub_chars, sub_lines, sub_box, sub_box_op, show_head, head_style, brand_tb, comp_dd, motion_dd,
                         intensity, seed_nb, trans_dd, trans_dur, vfade, min_dur, cover_dur, outro_mode,
                         target_dur, lead, pause_b, fill_cb]

        @safe
        def on_design_change(project_id, *vals):
            if not project_id:
                return ""
            p = need_project(project_id)
            (tpl, bg, acc, txt, sub, font, bold, tsize, ssize, spos, schars, slines, sbox, sbop, shead, hstyle, brand,
             comp, motion, inten, seed, trans, tdur, vf, mind, covd, omode, target, lead_v, pause_v, fill) = vals
            d = p.data["design"]
            d.update(template=tpl, bg_color=to_hex(bg), accent_color=to_hex(acc), text_color=to_hex(txt),
                     subtitle_color=to_hex(sub), font_file=font or "", font_weight_bold=bool(bold),
                     title_size=int(tsize), subtitle_size=int(ssize), subtitle_position=float(spos),
                     subtitle_max_chars=int(schars), subtitle_max_lines=int(slines), subtitle_box=bool(sbox),
                     subtitle_box_opacity=float(sbop), show_headlines=bool(shead), headline_style=hstyle or "plain",
                     brand_text=(brand or "").strip(), composition=comp, motion=motion, motion_intensity=float(inten),
                     seed=int(seed or 0), transition=trans, transition_duration=float(tdur), video_fade=float(vf),
                     min_slide_duration=float(mind), cover_duration=float(covd), outro_mode=omode)
            t = p.data["timing"]
            t.update(target_duration=float(target), lead_margin=float(lead_v), pause_between=float(pause_v),
                     fill_to_target=bool(fill))
            p.save()
            return "💾 Diseño guardado."

        for c in DESIGN_INPUTS:
            c.input(on_design_change, [pid] + DESIGN_INPUTS, [design_msg], show_progress="hidden")

        @safe
        def on_apply_template(project_id, tpl):
            p = need_project(project_id)
            p.data["design"].update(template_values(tpl))
            p.data["design"]["template"] = tpl
            p.save()
            return design_values(p)

        DESIGN_OUTPUTS = [tpl_dd, tpl_desc, c_bg, c_accent, c_text, c_sub, font_dd, bold_cb, title_size, sub_size,
                          sub_pos, sub_chars, sub_lines, sub_box, sub_box_op, show_head, head_style, brand_tb, comp_dd,
                          motion_dd, intensity, seed_nb, trans_dd, trans_dur, vfade, min_dur, cover_dur, outro_mode,
                          target_dur, lead, pause_b, fill_cb]
        btn_tpl.click(on_apply_template, [pid, tpl_dd], DESIGN_OUTPUTS)
        tpl_dd.change(lambda t: TEMPLATES.get(t, {}).get("description", ""), [tpl_dd], [tpl_desc])

        @safe
        def on_font_upload(project_id, file):
            if not file:
                return gr.update()
            src = Path(file if isinstance(file, str) else file.name)
            if src.suffix.lower() not in FONT_EXTENSIONS:
                raise UserError("La fuente debe ser .ttf u .otf.")
            dest = get_settings().fonts_path / src.name
            shutil.copyfile(src, dest)
            gr.Info(f"Fuente añadida: {src.name}. Comprueba su licencia antes de publicar.")
            return gr.update(choices=font_choices(), value=src.name)

        font_up.upload(on_font_upload, [pid, font_up], [font_dd])

        @safe
        def on_style_preview(project_id, text, slide_no, *vals):
            on_design_change(project_id, *vals)
            p = need_project(project_id)
            if not p.data["slides"]:
                raise UserError("Carga las imágenes primero.")
            out = p.cache_dir / "previews" / "style.png"
            img, warns = render_still(p, int(slide_no or 1) - 1, text or "Texto de prueba", out)
            for w in warns:
                gr.Warning(w)
            return str(img)

        btn_style.click(on_style_preview, [pid, style_text, style_slide] + DESIGN_INPUTS, [style_img])

        # ---------- audio ----------
        AUDIO_INPUTS = [music_dd, m_vol, v_vol, duck_cb, duck_str, fin, fout]

        @safe
        def on_audio_change(project_id, music, mv, vv, duck, dstr, fi, fo):
            if not project_id:
                return ""
            p = need_project(project_id)
            p.data["audio"].update(music_file=music or "", music_volume=float(mv), voice_volume=float(vv),
                                   ducking=bool(duck), ducking_strength=float(dstr), fade_in=float(fi), fade_out=float(fo))
            p.save()
            return "💾 Audio guardado."

        for c in AUDIO_INPUTS:
            c.input(on_audio_change, [pid] + AUDIO_INPUTS, [audio_msg], show_progress="hidden")
        btn_music_refresh.click(lambda: gr.update(choices=music_choices()), None, [music_dd])

        @safe
        def on_music_upload(file):
            if not file:
                return gr.update()
            src = Path(file if isinstance(file, str) else file.name)
            if src.suffix.lower() not in MUSIC_EXTENSIONS:
                raise UserError("Formato de audio no admitido.")
            dest = get_settings().music_path / src.name
            shutil.copyfile(src, dest)
            gr.Info(f"Pista añadida: {src.name}. Asegúrate de tener derechos para usarla.")
            return gr.update(choices=music_choices(), value=src.name)

        music_up.upload(on_music_upload, [music_up], [music_dd])

        @safe
        def on_mix(project_id):
            p = need_project(project_id)
            res = validate_project_script(p)
            if not res.ok:
                raise UserError("Completa el guion primero:\n" + "\n".join(res.errors))
            from jobs import Job
            job = Job("mezcla", p.id)
            out = preview_mix(p, job)
            return [out["wav"], f"Mezcla de {out['duration']:.1f} s · volumen medio {out['mean_db']:.1f} dB · "
                                f"pico {out['peak_db']:.1f} dB"]

        btn_mix.click(on_mix, [pid], [mix_audio, audio_msg], concurrency_limit=1)

        # ---------- exportación ----------
        @safe
        def on_export_settings(project_id, c, pr, f):
            if not project_id:
                return
            p = need_project(project_id)
            p.data["export"].update(crf=int(c), preset=pr, fps=int(f))
            p.save()

        for c in (crf, preset, fps_dd):
            c.input(on_export_settings, [pid, crf, preset, fps_dd], None, show_progress="hidden")

        def start_render(project_id, df, on, otext, ocard, preview: bool):
            p = need_project(project_id)
            apply_script_frame(p, df, on, otext, ocard)
            p.save()
            JOBS.start("previsualización" if preview else "vídeo final", p.id,
                       lambda job: render_project(pm().open(project_id), job, preview))
            return "<div class='sf-indet'>Trabajo iniciado…</div>"

        btn_prev.click(safe(lambda a, b, c, d, e: start_render(a, b, c, d, e, True)),
                       [pid, script_df, outro_on, outro_text, outro_card], [job_html], concurrency_limit=1)
        btn_final.click(safe(lambda a, b, c, d, e: start_render(a, b, c, d, e, False)),
                        [pid, script_df, outro_on, outro_text, outro_card], [job_html], concurrency_limit=1)

        def on_cancel():
            return "Cancelación solicitada." if JOBS.cancel() else "No hay ningún trabajo en curso."

        btn_cancel.click(on_cancel, None, [open_msg])

        def poll(shown):
            job = JOBS.current
            if job is None:
                return [gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), shown]
            phases = " → ".join([f"✅ {ph}" for ph in job.phases_done] + ([f"⏳ {job.phase}"] if job.running else []))
            if job.running:
                if job.phase_progress is not None:
                    pct = job.phase_progress * 100
                    bar = (f"<div class='sf-progress'><div style='width:{pct:.1f}%'>{html.escape(job.phase)}: "
                           f"{pct:.0f}% · {html.escape(job.detail)}</div></div>")
                else:
                    bar = f"<div class='sf-indet'>⏳ {html.escape(job.phase)}… {html.escape(job.detail)}</div>"
                status = f"<div><b>{html.escape(job.kind.capitalize())}</b> en curso · {job.elapsed:.0f} s</div>" + bar
            else:
                icon = {"completado": "✅", "error": "❌", "cancelado": "⏹"}.get(job.state, "")
                status = (f"<div class='sf-indet'>{icon} {html.escape(job.kind.capitalize())}: {job.state} "
                          f"en {job.elapsed:.0f} s</div>")
                if job.error:
                    status += f"<pre style='white-space:pre-wrap;color:#f85149'>{html.escape(job.error)}</pre>"
            status = f"<div style='font-size:13px;opacity:.9'>{html.escape(phases)}</div>" + status
            log_text = "\n".join(job.log_lines[-200:])
            if not job.running and job.state == "completado" and shown != job.id:
                rep = job.result
                post = rep.get("postflight", {})
                md = [f"### {'Previsualización' if rep.get('preview') else 'Vídeo final'} listo",
                      f"- 🎞 `{rep.get('output')}`", f"- 💬 `{rep.get('srt')}`"]
                if rep.get("thumbnail"):
                    md.append(f"- 🖼 `{rep.get('thumbnail')}`")
                md += [
                      f"- Duración: **{rep.get('duration', 0):.2f} s** (voz {rep.get('narration_total', 0):.2f} s)",
                      f"- Clips reutilizados de la caché: {rep.get('clips_reused')}/{rep.get('clips_total')}",
                      "\n**Verificación con ffprobe:**"]
                md += [f"- ✅ {i}" for i in post.get("info", [])] + [f"- ❌ {e}" for e in post.get("errors", [])]
                md += [f"- ⚠️ {w}" for w in rep.get("timeline_warnings", [])]
                tl = pd.DataFrame(rep.get("timeline", []))
                if not tl.empty:
                    tl.columns = ["#", "Imagen", "Inicio (s)", "Duración (s)", "Voz desde", "Voz hasta", "Movimiento"]
                return [status, log_text, rep.get("output"), "\n".join(md), tl, job.id]
            return [status, log_text, gr.update(), gr.update(), gr.update(), shown]

        # ---------- miniatura ----------
        def thumb_choices(p: Project):
            return [(f"{i} · {s['image']}", s["id"]) for i, s in enumerate(p.enabled_slides(), 1)]

        def thumb_preview(p: Project, guides: bool = True) -> str:
            cfg = {**default_thumbnail(), **(p.data.get("thumbnail") or {})}
            slide = default_source(p)
            img = make_cover(p.image_path(slide["image"]) if slide else None, p.data["design"],
                             default_title(p, slide), cfg.get("subtitle", ""), cfg.get("style", "banda"), guides)
            out = get_settings().cache_path / "miniaturas" / f"{p.id}_vista.jpg"
            return str(save_jpeg(img, out))

        def on_export_tab(project_id):
            keep = [gr.update()] * 7
            if not project_id:
                return keep
            try:
                p = need_project(project_id)
            except Exception:  # noqa: BLE001
                return keep
            cfg = {**default_thumbnail(), **(p.data.get("thumbnail") or {})}
            slide = default_source(p)
            try:
                prev = thumb_preview(p) if slide or cfg["style"] == "solo_texto" else None
                msg = ""
            except (ThumbnailError, OSError) as exc:
                prev, msg = None, f"⚠️ {exc}"
            return [gr.update(choices=thumb_choices(p), value=slide["id"] if slide else None),
                    default_title(p, slide), cfg.get("subtitle", ""), cfg.get("style", "banda"),
                    bool(cfg.get("auto", True)), prev, msg]

        TH_INPUTS = [pid, th_img, th_title, th_sub, th_style, th_auto]

        def _apply_thumb(project_id, img, title, sub, style, auto) -> Project:
            p = need_project(project_id)
            p.data["thumbnail"] = {"slide_id": img or "", "title": (title or "").strip(),
                                   "subtitle": (sub or "").strip(), "style": style or "banda", "auto": bool(auto)}
            p.save()
            return p

        @safe
        def on_th_preview(project_id, img, title, sub, style, auto):
            p = _apply_thumb(project_id, img, title, sub, style, auto)
            try:
                return [thumb_preview(p), ""]
            except ThumbnailError as exc:
                raise UserError(str(exc)) from exc

        @safe
        def on_th_save(project_id, img, title, sub, style, auto):
            p = _apply_thumb(project_id, img, title, sub, style, auto)
            from video_renderer import slugify
            dest = get_settings().outputs_path / p.id / f"{slugify(p.data.get('title') or p.name)}_miniatura.jpg"
            try:
                slide = default_source(p)
                img_ = make_cover(p.image_path(slide["image"]) if slide else None, p.data["design"],
                                  default_title(p, slide), p.data["thumbnail"]["subtitle"],
                                  p.data["thumbnail"]["style"], False)
                save_jpeg(img_, dest)
            except ThumbnailError as exc:
                raise UserError(str(exc)) from exc
            kb = dest.stat().st_size / 1024
            return [thumb_preview(p), f"✅ Miniatura guardada ({kb:.0f} KB): `{dest}`\n\nSúbela en YouTube Studio → "
                                      "Contenido → Shorts → ✏️ → Miniatura."]

        tab_export.select(on_export_tab, [pid], [th_img, th_title, th_sub, th_style, th_auto, th_preview, th_msg],
                          show_progress="hidden")
        btn_th_prev.click(on_th_preview, TH_INPUTS, [th_preview, th_msg])
        for comp in (th_img, th_style):
            comp.input(on_th_preview, TH_INPUTS, [th_preview, th_msg], show_progress="hidden")
        th_auto.input(on_th_preview, TH_INPUTS, [th_preview, th_msg], show_progress="hidden")
        for comp in (th_title, th_sub):
            comp.blur(on_th_preview, TH_INPUTS, [th_preview, th_msg], show_progress="hidden")
        btn_th_save.click(on_th_save, TH_INPUTS, [th_preview, th_msg])

        timer.tick(poll, [last_shown], [job_html, job_log, result_video, result_md, timeline_df, last_shown],
                   show_progress="hidden")

        @safe
        def on_open_out(project_id):
            d = get_settings().outputs_path / (project_id or "")
            return open_folder(d)

        btn_open_out.click(on_open_out, [pid], [open_msg])

        # ---------- ajustes ----------
        def ff_report(st: Settings) -> str:
            info = inspect_ffmpeg(st)
            if info.ok:
                return (f"✅ FFmpeg {info.version}\n\n- ffmpeg: `{info.ffmpeg}`\n- ffprobe: `{info.ffprobe}`\n"
                        "- libx264, AAC, libass, xfade y sidechaincompress disponibles.")
            return "❌ " + "\n\n❌ ".join(info.problems())

        @safe
        def on_check_ff(a, b):
            st = Settings.from_dict({**get_settings().to_dict(), "ffmpeg_path": a or "", "ffprobe_path": b or ""})
            return ff_report(st)

        btn_ff.click(on_check_ff, [ff_path, fp_path], [ff_msg])

        @safe
        def on_check_lm(url, model, vmodel, timeout):
            from script_manager import choose_models, lmstudio_model_info
            to = min(10.0, float(timeout or 10))
            info = [m for m in lmstudio_model_info(url, to) if m["type"] != "embeddings" and "embed" not in m["id"].lower()]
            if not info:
                return "⚠️ LM Studio responde, pero no hay ningún modelo de chat disponible."
            tipo = {"vlm": "👁 visión", "llm": "texto", "?": "tipo desconocido"}
            lines = [f"- `{m['id']}` · {tipo.get(m['type'], m['type'])}" + ("  ← **cargado**" if m["state"] == "loaded" else "")
                     for m in sorted(info, key=lambda m: (m["state"] != "loaded", m["type"] != "vlm"))]
            try:
                txt, vlm, notes = choose_models(url, model or "", vmodel or "", to, True)
                extra = f"\n\nSe usará `{vlm}` para leer las imágenes y `{txt}` para escribir el guion."
                extra += "".join(f"\n\n⚠️ {n}" for n in notes)
            except LLMError as exc:
                extra = f"\n\n⚠️ {exc}"
            return "✅ Conectado a LM Studio. Modelos:\n" + "\n".join(lines) + extra

        btn_lm.click(on_check_lm, [lm_url, lm_model, lm_vmodel, lm_to], [lm_msg])

        @safe
        def on_save_settings(a, b, url, model, vmodel, to, dp, do, dt, dc, da, dv):
            cur = get_settings().to_dict()
            cur.update(ffmpeg_path=a or "", ffprobe_path=b or "", lmstudio_url=url or "http://localhost:1234/v1",
                       lmstudio_model=model or "", lmstudio_vision_model=vmodel or "",
                       lmstudio_timeout=float(to or 300), projects_dir=dp or "",
                       outputs_dir=do or "", temp_dir=dt or "", cache_dir=dc or "", assets_dir=da or "",
                       voices_dir=dv or "")
            st = Settings.from_dict(cur)
            st.ensure_dirs()
            save_settings(st)
            return "✅ Ajustes guardados. Si has cambiado carpetas, reinicia la aplicación para servir sus archivos."

        btn_save_settings.click(on_save_settings, [ff_path, fp_path, lm_url, lm_model, lm_vmodel, lm_to, d_proj, d_out, d_tmp,
                                                   d_cache, d_assets, d_voices], [settings_msg])

        @safe
        def on_clear_cache():
            if JOBS.current is not None and JOBS.current.running:
                raise UserError("No se puede vaciar la caché durante un renderizado.")
            mb = clear_cache()
            return f"🧹 Caché vaciada ({mb:.1f} MB liberados)."

        btn_clear_cache.click(on_clear_cache, None, [settings_msg])
        btn_open_logs.click(lambda: open_folder(get_settings().logs_path), None, [settings_msg])

        @safe
        def on_gdl_update():
            try:
                ver = update_gallery_dl()
            except TikTokImportError as exc:
                raise UserError(str(exc)) from exc
            return f"✅ gallery-dl actualizado (versión {ver}). Reinicia la aplicación para usarlo."

        btn_gdl_update.click(on_gdl_update, None, [settings_msg], concurrency_limit=1)

        def env_report():
            st = get_settings()
            ok, msg = PiperEngine.is_available()
            kok, kmsg = KokoroEngine.is_available()
            voices = all_voices()
            font = resolve_font_path("", True)
            lines = [f"- Python {platform.python_version()} ({sys.executable})",
                     f"- Sistema: {platform.platform()}",
                     f"- Piper: {'✅ disponible' if ok else '❌ ' + msg}",
                     f"- Kokoro: {'✅ disponible' if kok else '❌ ' + kmsg}",
                     f"- Voces instaladas: {', '.join(v['engine'] + ':' + v['id'] for v in voices) or 'ninguna'}",
                     f"- Fuente predeterminada: `{font}`", ff_report(st).replace("\n\n", "\n")]
            return "\n".join(lines)

        btn_env.click(safe(env_report), None, [env_md])
        demo.load(lambda: [dl_info_md(config.DEFAULT_VOICE_ID), lexicon_frame(), gr.update(choices=voice_choices())],
                  None, [dl_info, lex_df, voice_dd])
    return demo


def allowed_paths(s: Settings) -> list[str]:
    return [str(p) for p in (s.projects_path, s.outputs_path, s.cache_path, s.temp_path, s.assets_path,
                             s.voices_path)]


def find_free_port(start: int, tries: int = 20) -> int:
    import socket
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"No hay ningún puerto libre entre {start} y {start + tries - 1}.")


def main() -> None:
    s = load_settings()
    s.ensure_dirs()
    log_file = setup_logging(s)
    log.info("%s %s · Python %s · %s", APP_NAME, APP_VERSION, platform.python_version(), platform.platform())

    def excepthook(exc_type, exc, tb):
        log.critical("Excepción no controlada:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = excepthook
    threading.excepthook = lambda a: log.critical("Excepción en hilo %s: %s", a.thread, a.exc_value)

    port = find_free_port(s.server_port)
    if port != s.server_port:
        msg = (f"El puerto {s.server_port} está ocupado (¿hay otra ventana de ShortsFactory abierta?). "
               f"Se usa el puerto {port}.")
        print(f"\n  AVISO: {msg}")
        log.warning(msg)
    demo = build_ui()
    demo.queue(default_concurrency_limit=4)
    print(f"\n  {APP_NAME} en http://127.0.0.1:{port}  (registro: {log_file})\n")
    demo.launch(server_name="127.0.0.1", server_port=port, inbrowser=s.open_browser, share=False,
                show_api=False, allowed_paths=allowed_paths(s), ssr_mode=False, favicon_path=None,
                show_error=True)


if __name__ == "__main__":
    main()
