"""Small Tkinter interface for the Instagram profile exporter."""
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import config
import main as exporter


TRAITS = (
    ("apertura_a_la_experiencia", "Apertura a la experiencia"),
    ("responsabilidad", "Responsabilidad"),
    ("extraversion", "Extraversión"),
    ("amabilidad", "Amabilidad"),
    ("neuroticismo", "Neuroticismo"),
)


class ExporterWindow:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.root.title("Exportador de Instagram")
        self.root.geometry("900x720")
        self.root.minsize(700, 560)

        self.username = tk.StringVar()
        self.limit = tk.StringVar(value="10")
        self.use_gemini = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Listo")

        self._build()
        self.root.after(120, self._poll_events)

    def _build(self):
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(4, weight=1)

        ttk.Label(container, text="Exportador de perfil de Instagram",
                  font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(container, text="Usa solo perfiles y datos que tengas autorización para analizar.").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 14)
        )

        ttk.Label(container, text="Usuario:").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self.username_entry = ttk.Entry(container, textvariable=self.username)
        self.username_entry.grid(row=2, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(container, text="Publicaciones/reels:").grid(row=2, column=2, sticky="e", padx=(0, 8))
        ttk.Spinbox(container, from_=1, to=500, textvariable=self.limit, width=7).grid(
            row=2, column=3, sticky="e"
        )

        ttk.Checkbutton(container, text="Solicitar análisis lingüístico de Gemini",
                        variable=self.use_gemini).grid(row=3, column=0, columnspan=2, sticky="w", pady=10)
        self.start_button = ttk.Button(container, text="Iniciar extracción", command=self._start)
        self.start_button.grid(row=3, column=2, sticky="e", pady=10)
        self.open_button = ttk.Button(container, text="Abrir carpeta de resultados", command=self._open_output,
                                      state="disabled")
        self.open_button.grid(row=3, column=3, sticky="e", pady=10)

        self.notebook = ttk.Notebook(container)
        self.notebook.grid(row=4, column=0, columnspan=4, sticky="nsew")
        summary_frame = ttk.Frame(self.notebook, padding=8)
        json_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(summary_frame, text="Resumen")
        self.notebook.add(json_frame, text="JSON completo")

        self.summary = scrolledtext.ScrolledText(summary_frame, wrap="word", height=20, state="disabled")
        self.summary.pack(fill="both", expand=True)
        self.json_view = scrolledtext.ScrolledText(json_frame, wrap="none", height=20, state="disabled")
        self.json_view.pack(fill="both", expand=True)

        ttk.Label(container, textvariable=self.status, anchor="w").grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0)
        )

    def _set_text(self, widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _start(self):
        handle = self.username.get().strip()
        if not handle:
            messagebox.showwarning("Falta el usuario", "Escribe el @handle de Instagram.", parent=self.root)
            return
        try:
            limit = int(self.limit.get())
            if limit < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Límite no válido", "Escribe un entero mayor que cero.", parent=self.root)
            return

        consent = False
        if self.use_gemini.get() and config.gemini_api_key():
            consent = messagebox.askyesno(
                "Enviar textos a Gemini",
                "Gemini recibirá la biografía y las descripciones seleccionadas para el análisis. "
                "¿Confirmas que tienes autorización y deseas enviarlas?",
                parent=self.root,
            )

        self.start_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.status.set("Iniciando…")
        self._set_text(self.summary, "")
        self._set_text(self.json_view, "")
        worker = threading.Thread(
            target=self._run,
            args=(handle, limit, consent),
            daemon=True,
        )
        worker.start()

    def _run(self, handle, limit, consent):
        try:
            report, path = exporter.run_export(
                handle,
                limit,
                gemini_consent=consent,
                progress=lambda message: self.events.put(("progress", message)),
            )
            self.events.put(("done", report, str(path)))
        except (Exception, SystemExit) as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self.status.set(event[1])
                elif event[0] == "done":
                    self._show_result(event[1], event[2])
                elif event[0] == "error":
                    self.status.set("La extracción terminó con un error.")
                    messagebox.showerror("No se pudo completar", event[1], parent=self.root)
                    self.start_button.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_events)

    def _show_result(self, report, path):
        profile = report.get("profile") or {}
        counts = profile.get("counts") or {}
        content = report.get("content") or {}
        items = content.get("items") or []
        media_paths = [
            file_path
            for item in items
            for file_path in (item.get("media_downloads") or {}).get("files", [])
        ]
        analysis = report.get("personality_analysis") or {}

        lines = [
            f"Perfil: @{profile.get('username') or report.get('target', '')}",
            f"Nombre: {profile.get('full_name') or 'no disponible'}",
            f"Seguidores: {counts.get('followers') if counts.get('followers') is not None else 'no disponible'}",
            f"Seguidos: {counts.get('following') if counts.get('following') is not None else 'no disponible'}",
            f"Publicaciones/reels en el informe: {len(items)} de {content.get('requested_limit', 0)}",
            f"Archivos multimedia descargados: {len(media_paths)}",
            f"Estado Gemini: {analysis.get('status', 'no disponible')}",
            f"Informe: {path}",
        ]
        result = analysis.get("result") or {}
        if result:
            lines.append("\nAnálisis lingüístico exploratorio:")
            for key, label in TRAITS:
                entry = result.get(key) or {}
                score = entry.get("score")
                score_text = f"{score}/100" if score is not None else "sin puntuación"
                justification = entry.get("justificacion") or ""
                lines.append(f"• {label}: {score_text}. {justification}")
            if result.get("limitaciones"):
                lines.append(f"Limitaciones: {result['limitaciones']}")
        elif analysis.get("error"):
            lines.append(f"Detalle Gemini: {analysis['error']}")

        self._set_text(self.summary, "\n".join(lines))
        self._set_text(self.json_view, json.dumps(report, ensure_ascii=False, indent=2))
        self.status.set("Proceso terminado.")
        self.start_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.notebook.select(0)

    def _open_output(self):
        folder = str(exporter.OUTPUT_DIR.resolve())
        if hasattr(os, "startfile"):
            os.startfile(folder)


if __name__ == "__main__":
    root = tk.Tk()
    ExporterWindow(root)
    root.mainloop()
