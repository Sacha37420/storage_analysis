"""Application fenêtrée : repérer, comprendre, nettoyer.

Trois vues sur le même nœud courant, et une table qui déclenche les
suppressions. Principe directeur du projet : **une vue pour repérer, une vue
pour agir**. Le treemap montre où est la masse ; la table donne les chiffres
exacts, la sélection et le bouton. Cliquer dans l'un déplace l'autre.

Le scan tourne dans un fil séparé : sur un disque rotatif il dure des minutes,
et une interface figée pendant ce temps serait inutilisable.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from dash import Dash, Input, Output, State, callback_context, dash_table, dcc, html, no_update

from ..core import scan, snapshot
from ..core.prune import AGGREGATE_PREFIX, prune
from ..core.tree import FileTree
from ..fmt import human
from . import figures, repos_view

# État serveur. L'application est locale et mono-utilisateur : un état de module
# est ici la solution simple et correcte, pas un raccourci.
_STATE: dict[str, Any] = {"tree": None, "job": None}

TABLE_ROWS = 400


# ------------------------------------------------------------------ scan --

class _ScanJob:
    """Scan en tâche de fond, avec progression consultable."""

    def __init__(self, path: str, size_mode: str) -> None:
        self.path = path
        self.size_mode = size_mode
        self.files = self.dirs = self.bytes = 0
        self.tree: FileTree | None = None
        self.error: str | None = None
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _progress(self, files: int, dirs: int, size: int) -> None:
        self.files, self.dirs, self.bytes = files, dirs, size

    def _run(self) -> None:
        try:
            self.tree = scan(self.path, size_mode=self.size_mode, on_progress=self._progress)
        except Exception as exc:                       # noqa: BLE001 - remonté à l'écran
            self.error = f"{type(exc).__name__} : {exc}"

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


# ------------------------------------------------------------- assistants --

def _tree() -> FileTree | None:
    return _STATE.get("tree")


def _safe_node(index: Any) -> int:
    tree = _tree()
    if tree is None:
        return 0
    try:
        value = int(index)
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value < len(tree) else 0


def _summary(tree: FileTree | None, node: int) -> list:
    if tree is None:
        return [html.Div("Aucune analyse en mémoire.", className="stat")]

    meta = tree.meta
    total = int(tree.total_size[node])
    return [
        html.Div([html.Span(human(total), className="value"),
                  html.Span("occupés", className="label")], className="stat hero"),
        html.Div([html.Span(f"{int(tree.file_count[node]):,}".replace(",", " "), className="value"),
                  html.Span("fichiers", className="label")], className="stat"),
        html.Div([html.Span(f"{max(int(tree.dir_count[node]) - 1, 0):,}".replace(",", " "),
                            className="value"),
                  html.Span("dossiers", className="label")], className="stat"),
        html.Div([html.Span(f"{meta.get('elapsed', 0):.1f} s".replace(".", ","), className="value"),
                  html.Span("de scan", className="label")], className="stat"),
        html.Div([html.Span("allouée" if meta.get("size_mode") == "allocated" else "logique",
                            className="value"),
                  html.Span("taille", className="label")], className="stat"),
    ]


def _rows(tree: FileTree | None, node: int) -> list[dict]:
    """Enfants directs du nœud courant, du plus gros au plus petit."""
    if tree is None:
        return []
    total = tree.total_size
    parent_total = int(total[node]) or 1
    rows = []
    for child in tree.children_by_size(node, limit=TABLE_ROWS).tolist():
        size = int(total[child])
        stamp = int(tree.mtime[child])
        rows.append({
            "index": child,
            "nom": tree.name(child) + (os.sep if tree.is_dir(child) else ""),
            "taille": human(size),
            "octets": size,
            "part": round(100 * size / parent_total, 1),
            "fichiers": int(tree.file_count[child]),
            "modifie": time.strftime("%d/%m/%Y", time.localtime(stamp)) if stamp else "—",
            "chemin": tree.path(child),
        })
    return rows


# ------------------------------------------------------------- structure --

def _layout() -> html.Div:
    return html.Div(
        id="root", className="app-root",
        children=[
            # --- barre supérieure ---
            html.Div(className="topbar", children=[
                html.H1("Analyseur d'espace disque"),
                html.Div(className="tabs", children=[
                    html.Button("Disque", id="mode-disk", className="tab active", n_clicks=0),
                    html.Button("Dépôts", id="mode-repos", className="tab", n_clicks=0),
                ]),
                dcc.Input(id="path", className="path-input", type="text",
                          placeholder=r"D:\  ou  C:\Users\moi  ou  un snapshot .npz",
                          value=os.path.expanduser("~"), debounce=True),
                html.Button("Analyser", id="scan-btn", className="btn primary", n_clicks=0),
                dcc.Dropdown(
                    id="size-mode", clearable=False, style={"width": "168px"},
                    options=[{"label": "Taille logique", "value": "logical"},
                             {"label": "Taille allouée", "value": "allocated"}],
                    value="logical",
                ),
                html.Button("Thème", id="theme-btn", className="btn ghost", n_clicks=0),
            ]),
            html.Div(id="progress-bar"),
            html.Div(id="summary", className="summary", children=_summary(None, 0)),

            # --- corps ---
            html.Div(id="disk-body", className="body", children=[
                html.Div(className="left", children=[
                    html.Div(className="toolbar", children=[
                        html.Button("↑ Remonter", id="up-btn", className="btn ghost", n_clicks=0),
                        html.Div(className="tabs", children=[
                            html.Button("Treemap", id="tab-treemap", className="tab active", n_clicks=0),
                            html.Button("Icicle", id="tab-icicle", className="tab", n_clicks=0),
                            html.Button("Extensions", id="tab-ext", className="tab", n_clicks=0),
                        ]),
                        dcc.Dropdown(
                            id="color-mode", clearable=False, style={"width": "168px"},
                            options=[{"label": "Couleur : profondeur", "value": "depth"},
                                     {"label": "Couleur : ancienneté", "value": "age"}],
                            value="depth",
                        ),
                        html.Div(id="crumb", className="crumb"),
                    ]),
                    html.Div(className="graph-wrap", children=[
                        dcc.Graph(id="chart", style={"height": "100%"},
                                  config={"displaylogo": False, "responsive": True,
                                          "modeBarButtonsToRemove": ["select2d", "lasso2d"]}),
                    ]),
                ]),

                html.Div(className="right", children=[
                    html.Div(className="toolbar", children=[
                        html.Span("Contenu du dossier courant — cochez pour nettoyer"),
                    ]),
                    html.Div(className="table-wrap", children=[
                        dash_table.DataTable(
                            id="table",
                            columns=[
                                {"name": "Nom", "id": "nom"},
                                {"name": "Taille", "id": "taille"},
                                {"name": "%", "id": "part", "type": "numeric"},
                                {"name": "Fichiers", "id": "fichiers", "type": "numeric"},
                                {"name": "Modifié", "id": "modifie"},
                            ],
                            data=[], row_selectable="multi", selected_rows=[],
                            sort_action="native", page_size=100,
                            style_as_list_view=True,
                            style_table={"overflowX": "auto"},
                            style_cell={"fontFamily": "Inter, Segoe UI, system-ui, sans-serif",
                                        "fontSize": "12.5px", "padding": "6px 8px",
                                        "textAlign": "left", "border": "none",
                                        "maxWidth": 240, "overflow": "hidden",
                                        "textOverflow": "ellipsis"},
                            style_cell_conditional=[
                                {"if": {"column_id": c},
                                 "textAlign": "right", "fontVariantNumeric": "tabular-nums"}
                                for c in ("taille", "part", "fichiers")
                            ],
                            style_header={"fontWeight": "600", "border": "none",
                                          "borderBottom": "1px solid var(--border)"},
                        ),
                    ]),
                    html.Div(className="cleanup", children=[
                        html.Div(id="selection", className="selection",
                                 children="Rien de sélectionné."),
                        html.Button("Ouvrir le dossier", id="open-btn", className="btn", n_clicks=0),
                        html.Button("Envoyer à la corbeille", id="trash-btn",
                                    className="btn danger", n_clicks=0, disabled=True),
                    ]),
                ]),
            ]),

            repos_view.layout(),

            html.Div(id="notice", className="notice"),

            dcc.ConfirmDialog(id="confirm"),
            dcc.Store(id="mode", data="disk"),
            dcc.Store(id="node", data=0),
            dcc.Store(id="theme", data="light"),
            dcc.Store(id="tab", data="treemap"),
            dcc.Store(id="version", data=0),
            dcc.Interval(id="poll", interval=400, disabled=True),
        ],
    )


# ------------------------------------------------------------- callbacks --

def _register(app: Dash) -> None:

    @app.callback(
        Output("root", "className"), Output("theme", "data"),
        Input("theme-btn", "n_clicks"), State("theme", "data"),
        prevent_initial_call=True,
    )
    def toggle_theme(_clicks, theme):
        new = "dark" if theme == "light" else "light"
        return f"app-root theme-{new}", new

    @app.callback(
        Output("mode", "data"),
        Output("mode-disk", "className"), Output("mode-repos", "className"),
        Output("summary", "style"), Output("disk-body", "style"),
        Output("repos-section", "style"),
        Input("mode-disk", "n_clicks"), Input("mode-repos", "n_clicks"),
        prevent_initial_call=True,
    )
    def switch_mode(_a, _b):
        repos = callback_context.triggered_id == "mode-repos"
        shown = {"display": "flex", "flex": "1 1 auto", "minHeight": 0,
                 "flexDirection": "column"}
        hidden = {"display": "none"}
        return (
            "repos" if repos else "disk",
            "tab" if repos else "tab active",
            "tab active" if repos else "tab",
            hidden if repos else {},
            hidden if repos else {"display": "flex", "flex": "1 1 auto", "minHeight": 0},
            shown if repos else hidden,
        )

    # --- lancement du scan ---
    @app.callback(
        Output("poll", "disabled"), Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("version", "data", allow_duplicate=True),
        Input("scan-btn", "n_clicks"),
        State("path", "value"), State("size-mode", "value"), State("version", "data"),
        prevent_initial_call=True,
    )
    def start(_clicks, path, size_mode, version):
        path = (path or "").strip().strip('"')
        if not path:
            return True, "Indiquez un dossier, un disque ou un snapshot .npz.", "notice warn", no_update

        if path.lower().endswith(".npz"):
            if not os.path.isfile(path):
                return True, f"Snapshot introuvable : {path}", "notice err", no_update
            try:
                _STATE["tree"] = snapshot.load(path)
            except Exception as exc:                    # noqa: BLE001
                return True, f"Snapshot illisible : {exc}", "notice err", no_update
            return True, f"Snapshot chargé : {path}", "notice ok", (version or 0) + 1

        if not os.path.isdir(path):
            return True, f"Dossier introuvable : {path}", "notice err", no_update

        _STATE["job"] = _ScanJob(path, size_mode or "logical")
        return False, f"Analyse de {path} en cours…", "notice", no_update

    # --- progression, puis résultat ---
    @app.callback(
        Output("poll", "disabled", allow_duplicate=True),
        Output("progress-bar", "children"),
        Output("notice", "children"), Output("notice", "className"),
        Output("version", "data"),
        Input("poll", "n_intervals"), State("version", "data"),
        prevent_initial_call=True,
    )
    def watch(_ticks, version):
        job: _ScanJob | None = _STATE.get("job")
        if job is None:
            return True, None, no_update, no_update, no_update

        if job.running:
            elapsed = time.perf_counter() - job.started
            rate = (job.files + job.dirs) / elapsed if elapsed > 0 else 0
            return (
                False,
                html.Div(className="progress", children=html.Div()),
                f"{job.files:,} fichiers · {job.dirs:,} dossiers · {human(job.bytes)} · "
                f"{int(rate):,}/s".replace(",", " "),
                "notice",
                no_update,
            )

        _STATE["job"] = None
        if job.error:
            return True, None, f"Échec du scan : {job.error}", "notice err", no_update

        _STATE["tree"] = job.tree
        meta = job.tree.meta if job.tree else {}
        errors = int(meta.get("errors", 0))
        message = f"Analyse terminée en {meta.get('elapsed', 0):.1f} s".replace(".", ",")
        if errors:
            message += f" — {errors} élément(s) inaccessible(s), non mesurés"
        return True, None, message, "notice ok" if not errors else "notice warn", (version or 0) + 1

    # --- nœud courant : clic dans la vue, ou remontée ---
    @app.callback(
        Output("node", "data"),
        Input("chart", "clickData"), Input("up-btn", "n_clicks"), Input("version", "data"),
        State("node", "data"), State("tab", "data"),
        prevent_initial_call=True,
    )
    def move(click_data, _up, _version, node, tab):
        tree = _tree()
        if tree is None:
            return 0

        trigger = callback_context.triggered_id
        if trigger == "version":
            return 0
        if trigger == "up-btn":
            current = _safe_node(node)
            return int(tree.parent[current]) if current else 0

        if trigger == "chart" and click_data and tab in ("treemap", "icicle"):
            point = (click_data.get("points") or [{}])[0]
            identifier = str(point.get("id", ""))
            if identifier.startswith(AGGREGATE_PREFIX):
                return no_update          # un nœud de synthèse n'est pas un lieu
            try:
                target = int(identifier)
            except ValueError:
                return no_update
            if 0 <= target < len(tree) and tree.is_dir(target):
                return target
        return no_update

    # --- onglets ---
    @app.callback(
        Output("tab", "data"),
        Output("tab-treemap", "className"), Output("tab-icicle", "className"),
        Output("tab-ext", "className"),
        Input("tab-treemap", "n_clicks"), Input("tab-icicle", "n_clicks"),
        Input("tab-ext", "n_clicks"),
        prevent_initial_call=True,
    )
    def switch(_a, _b, _c):
        chosen = {"tab-treemap": "treemap", "tab-icicle": "icicle",
                  "tab-ext": "extensions"}.get(callback_context.triggered_id, "treemap")
        return (chosen,
                "tab active" if chosen == "treemap" else "tab",
                "tab active" if chosen == "icicle" else "tab",
                "tab active" if chosen == "extensions" else "tab")

    # --- rendu ---
    @app.callback(
        Output("chart", "figure"), Output("summary", "children"),
        Output("crumb", "children"), Output("table", "data"),
        Output("table", "selected_rows"),
        Input("node", "data"), Input("theme", "data"), Input("tab", "data"),
        Input("color-mode", "value"), Input("version", "data"),
    )
    def render(node, theme, tab, color_mode, _version):
        tree = _tree()
        theme = theme or "light"
        if tree is None:
            return (figures.empty(theme, "Indiquez un dossier puis « Analyser »."),
                    _summary(None, 0), "", [], [])

        current = _safe_node(node)

        if tab == "extensions":
            figure = figures.extensions_bar(tree.extension_totals(current), theme)
        else:
            view = prune(tree, current)
            builder = figures.icicle if tab == "icicle" else figures.treemap
            figure = builder(view, theme, color_mode or "depth")

        return figure, _summary(tree, current), tree.path(current), _rows(tree, current), []

    # --- sélection et nettoyage ---
    @app.callback(
        Output("selection", "children"), Output("trash-btn", "disabled"),
        Input("table", "selected_rows"), State("table", "data"),
    )
    def selection(selected, data):
        if not selected or not data:
            return "Rien de sélectionné.", True
        chosen = [data[i] for i in selected if i < len(data)]
        total = sum(row["octets"] for row in chosen)
        return (
            html.Span([f"{len(chosen)} élément(s) sélectionné(s) — ", html.B(human(total)),
                       " à récupérer"]),
            False,
        )

    @app.callback(
        Output("confirm", "displayed"), Output("confirm", "message"),
        Input("trash-btn", "n_clicks"),
        State("table", "selected_rows"), State("table", "data"),
        prevent_initial_call=True,
    )
    def ask(_clicks, selected, data):
        if not selected or not data:
            return False, ""
        chosen = [data[i] for i in selected if i < len(data)]
        total = sum(row["octets"] for row in chosen)
        preview = "\n".join(f"  • {row['nom']}  ({row['taille']})" for row in chosen[:8])
        if len(chosen) > 8:
            preview += f"\n  … et {len(chosen) - 8} autre(s)"
        return True, (
            f"Envoyer {len(chosen)} élément(s) à la corbeille ?\n"
            f"{human(total)} seront récupérés.\n\n{preview}\n\n"
            "Rien n'est effacé définitivement : les éléments restent récupérables "
            "depuis la corbeille de Windows."
        )

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("version", "data", allow_duplicate=True),
        Input("confirm", "submit_n_clicks"),
        State("table", "selected_rows"), State("table", "data"), State("version", "data"),
        prevent_initial_call=True,
    )
    def do_delete(_submit, selected, data, version):
        tree = _tree()
        if tree is None or not selected or not data:
            return no_update, no_update, no_update

        from send2trash import send2trash

        freed, done, failures = 0, 0, []
        for position in selected:
            if position >= len(data):
                continue
            row = data[position]
            path = row["chemin"]
            # Garde-fou : jamais la racine d'un volume, ni le nœud courant.
            if os.path.dirname(path) == path:
                failures.append(f"{path} (racine de volume, refusé)")
                continue
            try:
                send2trash(path)
            except Exception as exc:                    # noqa: BLE001
                failures.append(f"{os.path.basename(path)} : {exc}")
                continue
            freed += tree.hide_subtree(int(row["index"]))
            done += 1

        if not done:
            return f"Aucune suppression : {'; '.join(failures)}", "notice err", no_update

        message = f"{done} élément(s) envoyé(s) à la corbeille — {human(freed)} récupérés"
        if failures:
            message += f" · {len(failures)} échec(s) : {'; '.join(failures[:3])}"
        return message, "notice warn" if failures else "notice ok", (version or 0) + 1

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Input("open-btn", "n_clicks"), State("node", "data"),
        prevent_initial_call=True,
    )
    def open_folder(_clicks, node):
        tree = _tree()
        if tree is None:
            return no_update, no_update
        path = tree.path(_safe_node(node))
        try:
            if os.name == "nt":
                os.startfile(path)                       # noqa: S606 - action explicite
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])     # noqa: S603,S607
        except OSError as exc:
            return f"Ouverture impossible : {exc}", "notice err"
        return f"Ouvert : {path}", "notice"


def create_app(snapshot_path: str | None = None) -> Dash:
    """Construit l'application. `snapshot_path` la précharge sans scanner."""
    if snapshot_path:
        _STATE["tree"] = snapshot.load(snapshot_path)

    app = Dash(
        __name__,
        title="Analyseur d'espace disque",
        update_title=None,
        assets_folder=os.path.join(os.path.dirname(__file__), "assets"),
    )
    app.layout = _layout
    _register(app)
    repos_view.register(app)
    return app
