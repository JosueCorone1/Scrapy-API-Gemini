"""Tkinter GUI for Big Five exports and Benford relationship analysis."""
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import benford
import config
import main as exporter

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:
    FigureCanvasTkAgg = None
    Figure = None


TRAITS = (
    ("apertura_a_la_experiencia", "Apertura a la experiencia"),
    ("responsabilidad", "Responsabilidad"),
    ("extraversion", "Extraversión"),
    ("amabilidad", "Amabilidad"),
    ("neuroticismo", "Neuroticismo"),
)

DATASETS = {
    "Seguidores de los seguidores": ("followers", "follower_count"),
    "Seguidos de los seguidos": ("following", "following_count"),
}


class TreeviewTooltip:
    """Display details for the row currently under the mouse pointer."""

    def __init__(self, tree):
        self.tree = tree
        self.details = {}
        self.window = None
        self.row = None
        self.tree.bind("<Motion>", self._on_motion, add="+")
        self.tree.bind("<Leave>", self._hide, add="+")
        self.tree.bind("<ButtonPress>", self._hide, add="+")

    def set_details(self, details):
        self.details = details
        self._hide()

    def _on_motion(self, event):
        row = self.tree.identify_row(event.y)
        if row == self.row:
            return
        self._hide()
        self.row = row
        detail = self.details.get(row)
        if not detail:
            return
        self.window = tk.Toplevel(self.tree)
        self.window.wm_overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(
            self.window,
            text=detail,
            justify="left",
            padx=9,
            pady=7,
            relief="solid",
            borderwidth=1,
            background="#fffbe6",
        ).pack()
        self.window.geometry(f"+{event.x_root + 14}+{event.y_root + 12}")

    def _hide(self, _event=None):
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.row = None


class ExporterWindow:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.root.title("Análisis de perfiles de Instagram")
        self.root.geometry("1220x880")
        self.root.minsize(980, 700)

        self.username = tk.StringVar()
        self.limit = tk.StringVar(value="10")
        self.use_gemini = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Listo")
        self.dataset = tk.StringVar(value=next(iter(DATASETS)))
        self.benford_data = None
        self.figure = None
        self.canvas = None

        self._build()
        self.root.after(120, self._poll_events)

    def _build(self):
        menu = tk.Menu(self.root)
        analysis_menu = tk.Menu(menu, tearoff=False)
        analysis_menu.add_command(
            label="Análisis de Personalidad (Big Five)",
            command=lambda: self.mode_notebook.select(self.personality_page),
        )
        analysis_menu.add_command(
            label="Análisis de Ley de Benford",
            command=lambda: self.mode_notebook.select(self.benford_page),
        )
        menu.add_cascade(label="Análisis", menu=analysis_menu)
        self.root.configure(menu=menu)

        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        ttk.Label(
            container,
            text="Análisis de perfiles de Instagram",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            container,
            text="Usa solo perfiles y datos que tengas autorización para analizar.",
        ).grid(row=1, column=0, sticky="w", pady=(3, 10))

        target = ttk.Frame(container)
        target.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="Usuario objetivo:").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.username_entry = ttk.Entry(target, textvariable=self.username)
        self.username_entry.grid(row=0, column=1, sticky="ew")

        self.mode_notebook = ttk.Notebook(container)
        self.mode_notebook.grid(row=3, column=0, sticky="nsew")
        self.personality_page = ttk.Frame(self.mode_notebook, padding=10)
        self.benford_page = ttk.Frame(self.mode_notebook, padding=10)
        self.mode_notebook.add(self.personality_page, text="1. Personalidad (Big Five)")
        self.mode_notebook.add(self.benford_page, text="2. Ley de Benford")
        self._build_personality_page()
        self._build_benford_page()

        ttk.Label(container, textvariable=self.status, anchor="w").grid(
            row=4, column=0, sticky="ew", pady=(8, 0)
        )

    def _build_personality_page(self):
        page = self.personality_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        controls = ttk.Frame(page)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Publicaciones/reels:").pack(side="left", padx=(0, 6))
        ttk.Spinbox(
            controls, from_=1, to=500, textvariable=self.limit, width=7
        ).pack(side="left")
        ttk.Checkbutton(
            controls,
            text="Solicitar análisis lingüístico de Gemini",
            variable=self.use_gemini,
        ).pack(side="left", padx=14)
        self.start_button = ttk.Button(
            controls, text="Iniciar análisis", command=self._start
        )
        self.start_button.pack(side="right", padx=(8, 0))
        self.open_button = ttk.Button(
            controls,
            text="Abrir carpeta de resultados",
            command=self._open_output,
            state="disabled",
        )
        self.open_button.pack(side="right")

        self.result_notebook = ttk.Notebook(page)
        self.result_notebook.grid(row=1, column=0, sticky="nsew")
        summary_frame = ttk.Frame(self.result_notebook, padding=8)
        json_frame = ttk.Frame(self.result_notebook, padding=8)
        self.result_notebook.add(summary_frame, text="Resumen")
        self.result_notebook.add(json_frame, text="JSON completo")
        self.summary = scrolledtext.ScrolledText(
            summary_frame, wrap="word", height=20, state="disabled"
        )
        self.summary.pack(fill="both", expand=True)
        self.json_view = scrolledtext.ScrolledText(
            json_frame, wrap="none", height=20, state="disabled"
        )
        self.json_view.pack(fill="both", expand=True)

    def _build_benford_page(self):
        page = self.benford_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)
        ttk.Label(
            page,
            text=(
                "Recopila las relaciones visibles para la sesión y los conteos disponibles "
                "de cada perfil. Un límite 403/429 detiene la extracción y conserva lo reunido."
            ),
            wraplength=1080,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        controls = ttk.Frame(page)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.benford_scan_button = ttk.Button(
            controls,
            text="Recopilar seguidores y seguidos",
            command=self._start_benford,
        )
        self.benford_scan_button.pack(side="left")
        ttk.Label(controls, text="Conjunto para Benford:").pack(
            side="left", padx=(18, 6)
        )
        self.dataset_box = ttk.Combobox(
            controls,
            textvariable=self.dataset,
            values=tuple(DATASETS),
            state="readonly",
            width=31,
        )
        self.dataset_box.pack(side="left")
        self.dataset_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._analyze_benford()
        )
        self.analyze_button = ttk.Button(
            controls,
            text="Calcular ajuste",
            command=self._analyze_benford,
            state="disabled",
        )
        self.analyze_button.pack(side="left", padx=(8, 0))
        self.open_benford_button = ttk.Button(
            controls,
            text="Abrir carpeta de resultados",
            command=self._open_output,
            state="disabled",
        )
        self.open_benford_button.pack(side="right")

        self.benford_summary = tk.StringVar(
            value="Recopila los datos para habilitar el análisis estadístico."
        )
        ttk.Label(
            page,
            textvariable=self.benford_summary,
            justify="left",
            wraplength=1080,
            padding=(8, 7),
            relief="groove",
        ).grid(row=2, column=0, sticky="ew", pady=(0, 8))

        self.benford_notebook = ttk.Notebook(page)
        self.benford_notebook.grid(row=3, column=0, sticky="nsew")
        self.followers_tree = self._make_user_table("Seguidores")
        self.following_tree = self._make_user_table("Seguidos")
        charts_page = ttk.Frame(self.benford_notebook, padding=6)
        self.benford_notebook.add(charts_page, text="Gráficos")
        if FigureCanvasTkAgg is None:
            ttk.Label(
                charts_page,
                text="Instala matplotlib para mostrar los gráficos.",
            ).pack(padx=20, pady=20)
        else:
            self.figure = Figure(figsize=(12, 3.8), dpi=90, constrained_layout=True)
            self.axes = self.figure.subplots(1, 3)
            self.canvas = FigureCanvasTkAgg(self.figure, master=charts_page)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self._draw_empty_charts()

    def _make_user_table(self, title):
        page = ttk.Frame(self.benford_notebook, padding=6)
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)
        columns = ("username", "full_name", "followers", "following")
        tree = ttk.Treeview(page, columns=columns, show="headings", height=16)
        for column, heading, width in (
            ("username", "Usuario", 190),
            ("full_name", "Nombre", 260),
            ("followers", "Seguidores", 130),
            ("following", "Seguidos", 130),
        ):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=90, anchor="w")
        y_scroll = ttk.Scrollbar(page, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(page, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tooltip = TreeviewTooltip(tree)
        if title == "Seguidores":
            self.followers_tooltip = tooltip
        else:
            self.following_tooltip = tooltip
        self.benford_notebook.add(page, text=title)
        return tree

    def _set_text(self, widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.benford_scan_button.configure(state=state)
        if busy:
            self.analyze_button.configure(state="disabled")
        elif self.benford_data:
            self.analyze_button.configure(state="normal")

    def _start(self):
        handle = self.username.get().strip()
        if not handle:
            messagebox.showwarning(
                "Falta el usuario",
                "Escribe el @handle de Instagram.",
                parent=self.root,
            )
            return
        try:
            limit = int(self.limit.get())
            if limit < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Límite no válido",
                "Escribe un entero mayor que cero.",
                parent=self.root,
            )
            return

        consent = False
        if self.use_gemini.get() and config.gemini_api_key():
            consent = messagebox.askyesno(
                "Enviar textos a Gemini",
                "Gemini recibirá la biografía y las descripciones seleccionadas para el análisis. "
                "¿Confirmas que tienes autorización y deseas enviarlas?",
                parent=self.root,
            )

        self._set_busy(True)
        self.open_button.configure(state="disabled")
        self.status.set("Iniciando análisis de personalidad…")
        self._set_text(self.summary, "")
        self._set_text(self.json_view, "")
        threading.Thread(
            target=self._run,
            args=(handle, limit, consent),
            daemon=True,
        ).start()

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

    def _start_benford(self):
        handle = self.username.get().strip()
        if not handle:
            messagebox.showwarning(
                "Falta el usuario",
                "Escribe el @handle de Instagram.",
                parent=self.root,
            )
            return
        self._set_busy(True)
        self.benford_data = None
        self.open_benford_button.configure(state="disabled")
        self.analyze_button.configure(state="disabled")
        self.benford_summary.set(
            "Consultando listas y perfiles con las pausas configuradas…"
        )
        self._populate_table(self.followers_tree, [], self.followers_tooltip)
        self._populate_table(self.following_tree, [], self.following_tooltip)
        self._draw_empty_charts()
        threading.Thread(
            target=self._run_benford,
            args=(handle,),
            daemon=True,
        ).start()

    def _run_benford(self, handle):
        try:
            data = exporter.run_benford_scan(
                handle,
                progress=lambda message: self.events.put(("progress", message)),
            )
            self.events.put(("benford_done", data))
        except (Exception, SystemExit) as exc:
            self.events.put(("benford_error", str(exc)))

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self.status.set(event[1])
                elif event[0] == "done":
                    self._show_result(event[1], event[2])
                elif event[0] == "error":
                    self.status.set("El análisis de personalidad terminó con un error.")
                    messagebox.showerror(
                        "No se pudo completar", event[1], parent=self.root
                    )
                    self._set_busy(False)
                elif event[0] == "benford_done":
                    self._show_benford_result(event[1])
                elif event[0] == "benford_error":
                    self.status.set("La extracción Benford terminó con un error.")
                    self.benford_summary.set(event[1])
                    messagebox.showerror(
                        "No se pudo recopilar", event[1], parent=self.root
                    )
                    self._set_busy(False)
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
        collection = content.get("collection") or {}
        interrupted = False
        for key, label in (("posts", "Publicaciones"), ("reels", "Reels")):
            details = collection.get(key) or {}
            if details.get("error"):
                interrupted = True
                lines.append(f"{label}: {details['error']}")
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
        self.status.set(
            "Recopilación interrumpida; consulta el detalle en el resumen."
            if interrupted else "Proceso terminado."
        )
        self._set_busy(False)
        self.open_button.configure(state="normal")
        self.mode_notebook.select(self.personality_page)
        self.result_notebook.select(0)

    def _show_benford_result(self, data):
        self.benford_data = data
        followers = data.get("followers", [])
        following = data.get("following", [])
        self._populate_table(self.followers_tree, followers, self.followers_tooltip)
        self._populate_table(self.following_tree, following, self.following_tooltip)
        self._set_busy(False)
        self.open_benford_button.configure(state="normal")
        self._analyze_benford()
        files = data.get("files") or {}
        csv_path = files.get("csv")
        collection = data.get("collection") or {}
        follower_status = collection.get("followers") or {}
        following_status = collection.get("following") or {}
        complete = (
            follower_status.get("complete")
            and following_status.get("complete")
            and not collection.get("metrics_interrupted")
        )
        label = "Recopilación completada" if complete else (
            "Recopilación interrumpida o incompleta; datos disponibles guardados"
        )
        status = (
            f"{label} para @{data.get('target')}: "
            f"{len(followers)} seguidores y {len(following)} seguidos."
        )
        if csv_path:
            status += f" CSV: {csv_path}"
        self.status.set(status)

    def _populate_table(self, tree, rows, tooltip):
        tree.delete(*tree.get_children())
        details = {}
        for index, row in enumerate(rows):
            iid = f"user_{index}"
            username = row.get("username") or row.get("id") or "desconocido"
            full_name = row.get("full_name") or ""
            follower_count = row.get("follower_count")
            following_count = row.get("following_count")
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    f"@{username}" if row.get("username") else username,
                    full_name,
                    self._format_count(follower_count),
                    self._format_count(following_count),
                ),
            )
            lines = [
                f"Usuario: @{username}" if row.get("username") else f"ID: {username}",
                f"Nombre: {full_name or 'no disponible'}",
                f"Seguidores: {self._format_count(follower_count)}",
                f"Seguidos: {self._format_count(following_count)}",
                f"Cuenta privada: {self._format_flag(row.get('is_private'))}",
                f"Verificada: {self._format_flag(row.get('is_verified'))}",
            ]
            if row.get("link"):
                lines.append(f"Perfil: {row['link']}")
            if row.get("stats_error"):
                lines.append(f"Detalle: {row['stats_error']}")
            details[iid] = "\n".join(lines)
        tooltip.set_details(details)

    @staticmethod
    def _format_count(value):
        return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "no disponible"

    @staticmethod
    def _format_flag(value):
        if value is True:
            return "sí"
        if value is False:
            return "no"
        return "no disponible"

    def _analyze_benford(self):
        if not self.benford_data:
            return
        relationship, metric = DATASETS.get(
            self.dataset.get(), next(iter(DATASETS.values()))
        )
        rows = self.benford_data.get(relationship, [])
        result = benford.analyze_counts(row.get(metric) for row in rows)
        sample_size = result["sample_size"]
        excluded = len(rows) - sample_size
        collection = (
            (self.benford_data.get("collection") or {}).get(relationship) or {}
        )
        state = "completa" if collection.get("complete") else "parcial o limitada"
        error = collection.get("error")
        collection_note = f" Detalle: {error}" if error else ""
        scan_collection = self.benford_data.get("collection") or {}
        metrics_note = (
            " Las consultas de conteos se interrumpieron ante un error."
            if scan_collection.get("metrics_interrupted")
            else ""
        )
        description = self.dataset.get()

        if not sample_size:
            self.benford_summary.set(
                f"{description}: no hay conteos válidos. Filas recibidas: "
                f"{len(rows)} (lista {state}).{metrics_note}{collection_note}"
            )
            self._draw_empty_charts()
            return

        statistic = result["chi_square"]
        p_value = result["p_value"]
        p_text = "< 1e-10" if p_value < 1e-10 else f"{p_value:.5g}"
        interpretation = (
            "difiere significativamente de Benford (α = 0,05)"
            if p_value < 0.05
            else "no muestra una diferencia significativa de Benford (α = 0,05)"
        )
        expected_min = min(result["expected"].values())
        caveat = (
            " Aviso: hay frecuencias esperadas menores que 5; la aproximación "
            "chi-cuadrado puede ser débil para esta muestra."
            if expected_min < 5 else ""
        )
        self.benford_summary.set(
            f"{description} — muestra válida: {sample_size}/{len(rows)} perfiles; "
            f"excluidos sin conteo válido: {excluded}; lista {state}.\n"
            f"χ² = {statistic:.4f}; gl = 8; p = {p_text}: {interpretation}.{caveat}\n"
            " Aviso: los conteos de seguidores están acotados y relacionados entre sí; "
            "el resultado es exploratorio y no permite concluir que una cuenta sea orgánica "
            "o anómala."
            f"{metrics_note}{collection_note}"
        )
        self._draw_charts(result)

    def _draw_empty_charts(self):
        if self.figure is None:
            return
        for axis in self.axes:
            axis.clear()
            axis.set_xticks(benford.DIGITS)
            axis.set_xlabel("Primer dígito significativo")
            axis.set_ylabel("Frecuencia (%)")
            axis.grid(axis="y", alpha=0.25)
        self.axes[0].set_title("Distribución teórica de Benford")
        self.axes[1].set_title("Distribución observada")
        self.axes[2].set_title("Comparación")
        self.canvas.draw_idle()

    def _draw_charts(self, result):
        if self.figure is None:
            return
        digits = benford.DIGITS
        proportions = result["proportions"]
        sample_size = result["sample_size"]
        expected = [proportions[digit] * 100 for digit in digits]
        observed = [
            result["observed"][digit] / sample_size * 100 for digit in digits
        ]
        for axis in self.axes:
            axis.clear()
            axis.set_xticks(digits)
            axis.set_xlabel("Primer dígito significativo")
            axis.set_ylabel("Frecuencia (%)")
            axis.grid(axis="y", alpha=0.25)
        self.axes[0].bar(digits, expected, color="#3678b7", alpha=0.82)
        self.axes[0].set_title("Distribución teórica de Benford")
        self.axes[1].bar(digits, observed, color="#e38a32", alpha=0.82)
        self.axes[1].set_title(f"Distribución observada (n={sample_size})")
        self.axes[2].bar(
            digits, expected, width=0.78, color="#3678b7", alpha=0.52, label="Benford"
        )
        self.axes[2].bar(
            digits, observed, width=0.42, color="#e38a32", alpha=0.78, label="Observada"
        )
        self.axes[2].set_title("Comparación superpuesta")
        self.axes[2].legend(fontsize=8)
        self.canvas.draw_idle()

    def _open_output(self):
        folder = str(exporter.OUTPUT_DIR.resolve())
        if hasattr(os, "startfile"):
            os.startfile(folder)


if __name__ == "__main__":
    root = tk.Tk()
    ExporterWindow(root)
    root.mainloop()
