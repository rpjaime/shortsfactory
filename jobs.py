"""Trabajos en segundo plano (renderizado, narración) con estado consultable.

Sólo se permite un trabajo pesado a la vez: un segundo intento devuelve un error
claro en lugar de lanzar otro renderizado igual en paralelo.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


class Cancelled(Exception):
    """El usuario canceló el trabajo."""


class JobBusy(Exception):
    """Ya hay un trabajo pesado en curso."""


@dataclass
class Job:
    kind: str
    project_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "pendiente"            # pendiente | en curso | completado | error | cancelado
    phase: str = ""
    phase_progress: float | None = None  # 0..1 sólo cuando se conoce el progreso real
    detail: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    log_lines: list[str] = field(default_factory=list)
    phases_done: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # -- API usada por el pipeline --
    def set_phase(self, name: str, detail: str = "") -> None:
        if self.phase and self.phase not in self.phases_done:
            self.phases_done.append(self.phase)
        self.phase = name
        self.phase_progress = None
        self.detail = detail
        self.log(f"▶ {name}" + (f": {detail}" if detail else ""))

    def progress(self, fraction: float | None, detail: str = "") -> None:
        self.phase_progress = None if fraction is None else max(0.0, min(1.0, fraction))
        if detail:
            self.detail = detail

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {msg}")
        if len(self.log_lines) > 500:
            self.log_lines = self.log_lines[-500:]
        log.info("[job %s] %s", self.id, msg)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    @property
    def running(self) -> bool:
        return self.state in ("pendiente", "en curso")

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.last: Job | None = None

    def start(self, kind: str, project_id: str, fn: Callable[[Job], dict[str, Any]]) -> Job:
        with self._lock:
            if self.current is not None and self.current.running:
                raise JobBusy(
                    f"Ya hay un trabajo en curso ({self.current.kind}, proyecto {self.current.project_id}). "
                    "Espera a que termine o cancélalo.")
            job = Job(kind=kind, project_id=project_id)
            self.current = job
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{job.id}", daemon=True)
        job.state = "en curso"
        thread.start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], dict[str, Any]]) -> None:
        try:
            job.result = fn(job) or {}
            job.state = "completado"
            if job.phase and job.phase not in job.phases_done:
                job.phases_done.append(job.phase)
            job.log("✔ Completado")
        except Cancelled:
            job.state = "cancelado"
            job.log("⏹ Cancelado por el usuario")
        except Exception as exc:  # noqa: BLE001 - ningún error debe cerrar la aplicación
            job.state = "error"
            job.error = str(exc) or exc.__class__.__name__
            job.log(f"✖ Error: {job.error}")
            log.error("Trabajo %s falló:\n%s", job.id, traceback.format_exc())
        finally:
            job.finished = time.time()
            with self._lock:
                self.last = job

    def cancel(self) -> bool:
        job = self.current
        if job is not None and job.running:
            job.cancel_event.set()
            job.log("Cancelación solicitada…")
            return True
        return False


JOBS = JobManager()
