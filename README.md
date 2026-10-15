# ShortsFactory

Aplicación local para Windows que convierte carruseles de imágenes en vídeos verticales para
**YouTube Shorts** (1080 × 1920, H.264 + AAC) con narración por voz sintética local, subtítulos
sincronizados con el audio real, animaciones sutiles y música de fondo opcional.

Todo se procesa en tu equipo: no hay cuentas, servicios de pago ni subidas de imágenes,
guiones o audios. Internet sólo se usa si **tú** pides descargar una voz (o para instalar las
dependencias la primera vez).

---

## 1. Requisitos

| Componente | Versión | Notas |
|---|---|---|
| Windows | 10 / 11 x64 | Probado en Windows 11 |
| Python | **3.11** | Versión probada con piper-tts 1.8.0 y gradio 5.50.0 |
| FFmpeg | ≥ 4.3, compilación completa | Necesita libx264, AAC, libass, `xfade` y `sidechaincompress` (la compilación «full» de gyan.dev los incluye) |
| Espacio | ~1,5 GB | Entorno virtual + voz + caché |

No se necesita GPU ni CUDA: la voz (onnxruntime) y el vídeo (libx264) funcionan en CPU.

Si te falta algo:

```bat
winget install --id Python.Python.3.11 -e
winget install --id Gyan.FFmpeg -e
```

## 2. Instalación

1. Copia la carpeta del proyecto donde quieras (por ejemplo `D:\Proyectos\OpenShorts`).
2. Haz doble clic en **`install.bat`**. El script:
   - localiza Python 3.11 (`py -3.11` o `python`) y se detiene con un mensaje claro si no está;
   - crea el entorno virtual `.venv`;
   - instala las dependencias **fijadas** de `requirements.txt` (todas con paquetes binarios para Windows);
   - comprueba que los módulos se importan y que FFmpeg está disponible;
   - ofrece descargar la voz española `es_ES-davefx-medium` (≈63 MB) desde la fuente oficial,
     verificando tamaño y MD5 (por defecto «Sí» a los 30 s);
   - ejecuta el diagnóstico final.
3. El registro completo queda en `logs\install.log`.

Comandos equivalentes a mano:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python tools\download_voice.py es_ES-davefx-medium
.venv\Scripts\python check_environment.py
```

## 3. Uso

- **`start.bat`** abre la aplicación en `http://127.0.0.1:7860` (sólo accesible desde tu equipo).
- **`check_environment.bat`** diagnostica Python, dependencias, FFmpeg, Piper, voces, fuentes,
  LM Studio y espacio libre (`logs\check_environment.txt`).
- **`run_tests.bat`** ejecuta las pruebas automáticas (`logs\tests.txt`).
- **`run_acceptance.bat`** ejecuta la prueba de aceptación completa con la voz real
  (`logs\acceptance.txt`).

### Guía para generar tu primer Short

1. **Proyectos** → escribe un nombre y pulsa *Crear proyecto*.
2. **Imágenes** → pega la ruta de la carpeta del carrusel (o pulsa *Examinar…*) y *Cargar*.
   Las imágenes se ordenan de forma natural (`2.png` antes que `10.png`). Haz clic en una
   miniatura para seleccionarla y usa *Subir*, *Bajar*, *Mover* o *Excluir*. La tabla avisa de
   imágenes ilegibles, pequeñas o duplicadas, de la orientación EXIF y de las transparencias.
   En *Ajustes de la diapositiva* puedes cambiar composición, escala, posición, zona visible y
   movimiento sólo para esa imagen.
   También puedes **importar un carrusel de TikTok** pegando su URL (desplegable «Importar desde
   TikTok»): se descargan sólo las imágenes, en orden, a `projects\<proyecto>\fuente_tiktok\<id>`,
   y el texto de la publicación se usa como contexto para el guion. Usa gallery-dl (GPL-2.0) como
   programa aparte; si TikTok cambia su web y deja de funcionar, pulsa *Ajustes → Actualizar
   gallery-dl*. Úsalo con tus publicaciones o con contenido que tengas permiso para reutilizar.
3. **Guion** → pulsa **🤖 Generar guion automáticamente** (LM Studio lee las imágenes y escribe la
   narración en la tabla) o escríbela a mano; también puedes importarla en JSON/SRT. Todo es
   editable en la tabla. Rol `silent` = diapositiva sin voz. *Validar* señala lo que falta.
4. **Voz** → elige la voz (Piper o Kokoro), escúchala con *Probar esta voz*, ajusta velocidad
   (0,85–1,15) y pausa entre frases, y pulsa *Generar narración*. Verás la duración real de cada
   fragmento y podrás escucharlo. En *Pronunciaciones* corriges cómo lee la voz términos como
   «GitHub» (→ «guítjab») sin cambiar los subtítulos.
5. **Diseño** → elige plantilla (Tech Dark, Clean Editorial, High Energy) y personaliza colores,
   tipografía, tamaños, posición de subtítulos, movimiento, transiciones y ritmo.
   *Previsualizar estilo* muestra un fotograma con el subtítulo renderizado por libass, tal cual
   saldrá en el vídeo.
6. **Audio** → elige una pista de `assets\music` (o súbela), volumen, atenuación bajo la voz y
   fundidos. *Escuchar mezcla de prueba* genera sólo el audio.
7. **Exportación** → *Previsualización rápida* (540 × 960) y después *Renderizar vídeo final*.
   La barra de progreso refleja el progreso real (fotogramas generados y tiempo codificado por
   FFmpeg); las fases sin progreso medible sólo muestran su estado. Al terminar verás el vídeo, el
   informe de ffprobe y el botón para abrir la carpeta de salida.

Los vídeos se guardan en `outputs\<proyecto>\` junto con su `.srt` y un `.report.json`.

## 4. Cómo funciona

| Módulo | Responsabilidad |
|---|---|
| `app.py` | Interfaz Gradio (8 pestañas), trabajos en segundo plano, guardado automático |
| `config.py` | Valores predeterminados, `settings.json`, localización de FFmpeg, registro |
| `project_manager.py` | Proyectos versionados (`schema_version`), migraciones, copia de seguridad, rutas seguras |
| `image_processor.py` | Orden natural, análisis, EXIF, transparencias, composición 9:16 y movimiento |
| `script_manager.py` | Validación, importación/exportación JSON y SRT, cliente LM Studio |
| `tts_engine.py` | Interfaz común de voz, proveedor Piper, caché y descarga verificada de voces |
| `timeline.py` | Duraciones a partir del audio real, transiciones sin doble conteo |
| `subtitle_engine.py` | Segmentación legible, ASS (estilo) y SRT |
| `audio_mixer.py` | Mezcla voz + música con atenuación automática y limitador |
| `video_renderer.py` | Clips por diapositiva (con caché), montaje `xfade`, subtítulos, codificación |
| `quality_checks.py` | Validaciones previas y verificación del MP4 con ffprobe |
| `jobs.py` | Un único trabajo pesado a la vez, cancelación |

**Sincronización.** Cada frase se sintetiza por separado y se mide su duración real; los
silencios iniciales y finales se recortan. La diapositiva dura lo que necesita su narración
(más márgenes y pausa), nunca se corta el audio y la voz no se acelera para cuadrar un tiempo
objetivo: si se supera, la aplicación avisa y propone alternativas. Los subtítulos se colocan
frase a frase con los tiempos reales; si una frase no cabe en pantalla se divide por pausas
naturales (comas, punto y coma) y su tiempo se reparte en proporción al texto. No se simula
sincronización palabra a palabra.

**Transiciones.** El clip *i* dura `hueco_i + transición_salida`; las transiciones se solapan
con la cola del clip anterior, así que la duración total es la suma de los huecos (sin contar
dos veces los solapamientos). La narración empieza cuando termina la transición de entrada.

**Caché.** El audio se reutiliza si no cambian texto, voz (incluido su archivo) ni parámetros.
Los clips por diapositiva se reutilizan si no cambian imagen, diseño, ajustes, movimiento ni
duración. *Ajustes → Vaciar caché* la borra sin afectar a los proyectos.

**Seguridad de rutas.** Las referencias a imágenes son nombres de archivo dentro de la carpeta
del proyecto; se rechazan rutas absolutas, `..` y separadores manipulados. Los comandos de
FFmpeg se construyen como listas de argumentos, nunca concatenando cadenas de shell.

## 5. Voz española (Piper)

- Motor: [piper-tts 1.8.0](https://github.com/OHF-voice/piper1-gpl) (licencia **GPL-3.0-or-later**).
- Voces verificadas (repositorio oficial `rhasspy/piper-voices`, etiqueta `v1.0.0`):
  - `es_ES-davefx-medium` — masculina; datos CC0, ajuste fino desde la voz inglesa *lessac*.
  - `es_ES-sharvard-medium` — dos locutores; datos **CC BY 3.0** (exige atribución).
- Descarga desde la pestaña **Voz** o con `tools\download_voice.py`. Los archivos se guardan en
  `assets\voices` (configurable) y se verifican contra el índice `voices.json`.
- Para añadir otra voz manualmente, copia su `.onnx` y su `.onnx.json` en `assets\voices`.

> ⚖️ Antes de publicar contenido comercial, revisa la ficha (MODEL_CARD) y la licencia de la voz
> utilizada, así como la licencia de la música y de las fuentes.

## 6. Guion automático con LM Studio (opcional)

1. Abre LM Studio, carga un modelo **con visión** (por ejemplo Gemma 3/4 o Qwen-VL; LM Studio los
   marca como «vision») y activa el servidor local (*Developer → Start Server*, puerto 1234).
2. En **Ajustes**, pulsa *Comprobar conexión*: verás qué modelos están cargados y cuáles tienen
   visión, y cuál se usará. Puedes fijar el modelo de texto y el de visión o dejarlos en automático
   (se prefiere el que ya está cargado y se usa el mismo para ambas tareas).
3. En **Guion**, pulsa *Generar guion automáticamente*:
   - la IA lee cada imagen (texto visible, nombre del recurso y descripción) y lo guarda en
     «Notas para la IA» con la marca `[Leído de la imagen]`;
   - después escribe el gancho, una narración por diapositiva y el cierre, y los coloca en la tabla;
   - las narraciones que ya tenías no se tocan salvo que marques *Sobrescribir*; las notas que
     escribas tú tienen prioridad sobre la lectura automática;
   - las lecturas se reutilizan mientras la imagen no cambie.
4. Revisa y edita el texto en la tabla. El modelo tiene instrucciones de no inventar
   características, estadísticas, estrellas ni licencias que no aparezcan en las imágenes o en
   tus notas, pero revisa siempre los datos antes de publicar.

Sólo se permiten URLs locales o de red privada (localhost, 127.0.0.1, 192.168.x.x…), y las
peticiones ignoran cualquier proxy del sistema.

## 6b. Voces: Piper y Kokoro

- **Piper** (`es_ES-davefx-medium`, `es_ES-sharvard-medium`): ligera y rápida.
- **Kokoro-82M** (Apache-2.0): más natural; voces españolas Dora (femenina), Alex y Santa
  (masculinas). Se descarga desde la pestaña Voz (≈354 MB, publicación oficial de kokoro-onnx,
  verificación SHA-256) en `assets\voices\kokoro`.
- **Pronunciaciones**: `assets\pronunciaciones.json`, editable desde la pestaña Voz. Afecta sólo
  al audio; los subtítulos muestran el texto original.

## 6b-bis. Voz clonada (tu propia voz)

- Motor: **Chatterbox Multilingual** (MIT, Resemble AI), ajuste fino *Spanish as spoken in Spain*
  (`t3_es_es`) con su decodificador v3, `cfg 0,8` y exageración 0,5 (elegidos escuchando pruebas).
- Instalación aparte: `install_voz_clonada.bat` (entorno `.venv-voz`, PyTorch CPU y ~4 GB de modelos).
- En la pestaña **Voz → Voz clonada** graba o sube 15-30 s de tu voz; aparece como «Voz clonada · nombre».
- Funciona en CPU (la RX 7800 XT aún no tiene PyTorch-ROCm en Windows): ~4 s de cálculo por segundo de
  voz y ~30 s de carga la primera vez de cada sesión. Todo se guarda en caché.
- No admite cambiar la velocidad. Todo el audio lleva la marca de agua inaudible PerTh de Resemble AI.
- Usa sólo tu voz o la de alguien que te haya dado permiso expreso.
- Herramientas de prueba: `probar_voz_clonada.bat`, `comparar_voz_clonada.bat`, `comparar_decodificador_espana.bat`.

## 6c. Privacidad

La aplicación desactiva la telemetría de onnxruntime (`ORT_DISABLE_TELEMETRY=1`), Gradio y
Hugging Face Hub. Las únicas conexiones de red son las que pides explícitamente (descargar una
voz) y las peticiones a tu LM Studio local.

## 6d. Miniatura (portada del Short)

En **Exportación → 🖼 Miniatura** eliges la imagen de fondo, el titular (las palabras \*entre asteriscos\* se
resaltan con el color de acento), una etiqueta opcional y uno de tres estilos. Se genera un JPG 1080×1920 de
menos de 2 MB. YouTube recorta la miniatura de los Shorts a una franja central de proporción ≈3:2 en la página
principal y en el canal, así que el titular siempre se coloca dentro de esa franja (líneas amarillas en la vista
previa; no aparecen en el archivo). Con la casilla activada se crea automáticamente junto a cada vídeo final
(`<nombre>_miniatura.jpg`). Para subirla: YouTube Studio → Contenido → Shorts → ✏️ → Miniatura (desde
ordenador; la función de miniatura personalizada en Shorts está llegando primero a canales del Programa de
Partners).

## 7. Música y fuentes

- Copia tus pistas en `assets\music` (mp3, wav, ogg, flac, m4a, aac, opus) o súbelas desde la
  pestaña Audio. Si la pista es más corta que el vídeo, se repite; si es más larga, se recorta
  con fundido. **No se incluye música** en el proyecto.
- Copia fuentes `.ttf`/`.otf` en `assets\fonts` (o súbelas desde Diseño). Sin fuentes propias se
  usa Segoe UI/Arial del sistema. Comprueba que incluyan acentos y `¿¡ñ` (el diagnóstico lo
  verifica).

## 8. Configuración

`settings.json` (no versionado) guarda las rutas de FFmpeg/ffprobe, LM Studio y las carpetas de
proyectos, salida, temporales, caché, recursos y voces. Se edita desde **Ajustes**; una ruta
vacía significa la carpeta predeterminada dentro de la aplicación.

## 9. Solución de problemas

| Síntoma | Solución |
|---|---|
| `install.bat`: «no se encuentra Python 3.11» | `winget install --id Python.Python.3.11 -e` y vuelve a ejecutarlo |
| Fallo al instalar dependencias | Revisa `logs\install.log`; borra `.venv` y repite. Hace falta Internet la primera vez |
| «No se encuentra FFmpeg» | `winget install --id Gyan.FFmpeg -e` o indica la ruta en Ajustes |
| «Tu FFmpeg no incluye…» | Usa la compilación «full» de gyan.dev |
| «La voz … no está instalada» | Pestaña Voz → *Descargar y verificar*, o `tools\download_voice.py` |
| «modelo incompatible o dañado» | Borra los archivos de esa voz en `assets\voices` y descárgala de nuevo |
| «Ya hay un trabajo en curso» | Espera o pulsa *Cancelar* en Exportación |
| La duración supera la objetivo | Amplía la duración objetivo, acorta el guion o sube ligeramente la velocidad |
| Subtítulos con caracteres raros | Usa una fuente con glifos españoles (el diagnóstico lo indica) |
| El selector *Examinar…* no aparece | Pega la ruta de la carpeta en el cuadro de texto |
| LM Studio «No se puede conectar» | Inicia el servidor local en LM Studio y revisa el puerto |
| Cualquier otro error | `logs\shortsfactory.log` y, si falló FFmpeg, `logs\ffmpeg_*.log` |

## 10. Pruebas

`run_tests.bat` (o `.venv\Scripts\python -m pytest -q`) ejecuta las pruebas con imágenes y audios
sintéticos y un motor de voz de prueba, sin descargar modelos: orden natural, validación JSON,
rutas, duraciones, sincronización de subtítulos, solapamientos, mezcla de audio, comandos de
FFmpeg, validación del MP4, errores de Piper y recuperación de proyectos. Las pruebas de vídeo y
audio necesitan FFmpeg en el PATH (se omiten automáticamente si no está).

`run_acceptance.bat` recorre los criterios de aceptación 3–15 con la voz real de Piper.

## 11. Limitaciones conocidas

- La codificación usa libx264 en CPU; no se usa el codificador por hardware de AMD (AMF).
- Los subtítulos se sincronizan por frase (tiempos reales); dentro de una frase larga, los bloques
  se reparten en proporción al texto.
- El modo de composición «llenar» se rechaza automáticamente si recortaría más de un 22 % de la
  imagen (salvo que lo fuerces por diapositiva). No se detecta texto dentro de las imágenes.
- La previsualización del estilo usa la diapositiva elegida y un texto de ejemplo.

## 12. Licencia

El código de ShortsFactory se publica bajo licencia **MIT** (ver `LICENSE`).

Las dependencias y los modelos se instalan aparte y mantienen sus propias licencias: piper-tts
(GPL-3.0-or-later), Chatterbox (MIT), Kokoro, gallery-dl (GPL-2.0), FFmpeg y las voces, músicas y
fuentes que elijas. Revísalas antes de usar los vídeos con fines comerciales.

---

Proyecto de [Jaime Romero](https://jaimeromero.eu/proyectos/shortsfactory/).
