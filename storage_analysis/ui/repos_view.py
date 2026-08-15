"""Section « Dépôts » de l'application.

Le lien avec l'analyse disque est direct : un clone occupe de la place, mais
savoir s'il est *supprimable* dépend de ce qui existe ailleurs — poussé,
archivé, référencé comme submodule. Cette vue met les deux informations côte à
côte et n'autorise l'action que lorsque rien ne s'y oppose.

Le catalogue est construit dans un fil séparé, comme le scan : mesurer plusieurs
Go de clones sur un disque rotatif prend des dizaines de secondes.
"""

from __future__ import annotations

import threading
from typing import Any

from dash import Input, Output, State, callback_context, dash_table, dcc, html, no_update

from ..fmt import human, since
from ..forge import actions, cache, local
from ..forge.catalog import Catalog, build_catalog
from ..forge.clients import ForgeError
from ..forge.config import build_clients, load_config
from . import figures

_STATE: dict[str, Any] = {"catalog": None, "job": None}


# ------------------------------------------------------------ construction --

class _CatalogJob:
    """Construction du catalogue en tâche de fond."""

    kind = "catalog"

    def __init__(self, roots: list[str], offline: bool) -> None:
        self.roots = roots
        self.offline = offline
        self.done = self.total = 0
        self.step = "recherche des dépôts…"
        self.catalog: Catalog | None = None
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            paths = local.discover(self.roots)
            if not paths:
                self.error = f"Aucun dépôt git sous {', '.join(self.roots)}"
                return
            self.total = len(paths)

            def progress(done: int, total: int, name: str) -> None:
                self.done, self.step = done, f"lecture de {name}"

            repos = local.read_repos(paths, on_progress=progress)

            remotes, queried = [], set()
            if not self.offline:
                self.step = "interrogation des forges…"
                for client in build_clients():
                    try:
                        remotes.extend(client.list_repos())
                        queried.add(client.host)
                    except ForgeError as exc:
                        self.step = str(exc)

            self.catalog = build_catalog(repos, remotes, queried)
            cache.save(self.catalog, self.roots)
        except Exception as exc:                        # noqa: BLE001 - remonté à l'écran
            self.error = f"{type(exc).__name__} : {exc}"

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


class _LoginJob:
    """Connexion à une forge, dans un fil séparé : le flux ouvre un navigateur."""

    kind = "login"

    def __init__(self, slug: str = "github") -> None:
        self.slug = slug
        self.step = "ouverture du navigateur…"
        self.error: str | None = None
        self.message = ""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import webbrowser

        from ..env import set_env_values
        from ..forge import hub, oauth
        from ..forge.config import reset_token_cache

        config = load_config()
        hub_config = config.hub()
        try:
            if hub_config is not None:
                self.step = "authentification sur le lab…"
                tokens = hub.login(hub_config, on_prompt=webbrowser.open)
                if tokens.refresh_token:
                    set_env_values({"LAB_REFRESH_TOKEN": tokens.refresh_token})
                self.step = f"liaison du compte {self.slug}…"
                payload = hub.site_token(hub_config, tokens.access_token, self.slug,
                                         on_prompt=webbrowser.open)
                _STATE["providers"] = None      # la liste est à recharger
                self.message = (f"{self.slug} relié via oauth-hub — portées : "
                                f"{payload.get('scope') or 'non déclarées'}")
            elif config.github_client_id:
                self.step = "autorisation GitHub…"
                result = oauth.pkce_login(config.github_client_id, oauth.build_scopes(),
                                          on_prompt=webbrowser.open)
                set_env_values({
                    "GITHUB_TOKEN": result.token,
                    "GITHUB_TOKEN_SOURCE": "oauth-pkce",
                    "GITHUB_TOKEN_SCOPES": " ".join(result.scopes),
                })
                self.message = f"Connecté à GitHub — portées : {' '.join(result.scopes)}"
            else:
                self.error = (
                    "Adresse du lab manquante. Une application de bureau ne peut pas "
                    "deviner où joindre oauth-hub : renseignez le champ « Lab » "
                    "ci-dessus (le domaine seul, ex. mon-lab.exemple.fr) puis "
                    "Enregistrer. À défaut de lab : GITHUB_CLIENT_ID dans .env pour "
                    "une connexion directe, ou un jeton personnel dans GITHUB_TOKEN."
                )
                return
            reset_token_cache()
        except Exception as exc:                        # noqa: BLE001 - remonté à l'écran
            self.error = f"{type(exc).__name__} : {exc}"

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


def _catalog() -> Catalog | None:
    """Catalogue en mémoire, rechargé dès que le cache disque a changé.

    Sans le contrôle de date, un catalogue chargé au démarrage resterait figé
    pour toute la vie du processus : une reconstruction faite ailleurs — par la
    ligne de commande, ou par une autre fenêtre — n'apparaîtrait jamais.
    """
    try:
        stamp = cache.CACHE_PATH.stat().st_mtime
    except OSError:
        stamp = None

    if _STATE.get("catalog") is None or stamp != _STATE.get("catalog_stamp"):
        cached = cache.load(max_age=None)
        if cached is not None:
            _STATE["catalog"] = cached[0]
            _STATE["catalog_stamp"] = stamp
    return _STATE.get("catalog")


# ------------------------------------------------------------------ données --

FILTERS = {
    "all": "Tous les dépôts",
    "local": "Clonés localement",
    "remote": "Distants non clonés",
    "archived": "Archivés",
    "free": "Libérables",
}


def _sources(catalog: Catalog | None) -> list[dict]:
    """Forges présentes au catalogue : une par hôte, plus les dépôts sans remote."""
    options = [{"label": "Toutes les forges", "value": "*"}]
    if catalog is None:
        return options
    hosts = sorted({e.ref.host for e in catalog.entries.values() if e.ref is not None})
    options += [{"label": h, "value": h} for h in hosts]
    if any(e.ref is None for e in catalog.entries.values()):
        options.append({"label": "sans remote", "value": "-"})
    return options


def _from_source(entry, source: str) -> bool:
    if source in ("*", "", None):
        return True
    if source == "-":
        return entry.ref is None
    return entry.ref is not None and entry.ref.host == source


def _keep(entry, catalog: Catalog, kind: str) -> bool:
    if kind == "local":
        return entry.local is not None
    if kind == "remote":
        return entry.local is None and entry.remote is not None
    if kind == "archived":
        return entry.archived
    if kind == "free":
        return entry.local is not None and not catalog.blockers(entry)
    return True


def _remote_note(entry) -> str:
    """Ce que la forge dit du projet, indépendamment du disque."""
    remote = entry.remote
    if remote is None:
        return "—" if entry.ref is None else "non interrogé"
    marks = ["archivé" if remote.archived else "actif",
             "privé" if remote.private else "public"]
    if remote.fork:
        marks.append("fork")
    return " · ".join(marks)


def _rows(catalog: Catalog | None, kind: str = "all", source: str = "*") -> list[dict]:
    if catalog is None:
        return []
    rows = []
    for entry in catalog.sorted_entries("size"):
        if not _keep(entry, catalog, kind) or not _from_source(entry, source):
            continue
        blockers = catalog.blockers(entry)
        local_repo = entry.local
        rows.append({
            "cle": entry.key,
            "nom": entry.name,
            "etat": entry.status + (" · archivé" if entry.archived else ""),
            "taille": human(entry.size_bytes) if entry.size_bytes else "—",
            "octets": entry.size_bytes,
            "git": human(local_repo.git_dir_bytes) if local_repo and local_repo.git_dir_bytes else "—",
            "activite": since(entry.last_activity),
            "distant": _remote_note(entry),
            "motif": " ; ".join(blockers),
            "libre": 1 if (not blockers and local_repo is not None) else 0,
            "chemin": local_repo.path if local_repo else "",
        })
    return rows


def _graph_nodes(catalog: Catalog | None) -> list[dict]:
    """Aplatit le graphe de submodules pour un pavage hiérarchique."""
    if catalog is None or not catalog.edges:
        return []

    nodes: list[dict] = []
    seen: set[str] = set()

    def size_of(key: str) -> int:
        entry = catalog.entries.get(key)
        return entry.size_bytes if entry else 0

    for root_key in catalog.roots():
        if root_key in seen:
            continue
        seen.add(root_key)
        entry = catalog.entries.get(root_key)
        nodes.append({
            "id": root_key, "label": entry.name if entry else root_key, "parent": "",
            "value": size_of(root_key), "depth": 0,
            "note": f"{human(size_of(root_key))} · racine",
        })
        for edge in catalog.children_of(root_key):
            child_key = edge.child_key or edge.url
            node_id = f"{root_key}//{child_key}"
            state = "déployé" if edge.checked_out else (
                "déclaré, non déployé" if edge.child_known else "hors catalogue")
            nodes.append({
                "id": node_id,
                "label": (catalog.entries[child_key].name
                          if edge.child_known and child_key in catalog.entries
                          else child_key.rsplit("/", 1)[-1]),
                "parent": root_key,
                "value": max(size_of(child_key), 1),
                "depth": 1,
                "note": f"{edge.rel_path or '?'} · {state}",
            })
    return nodes


def _summary(catalog: Catalog | None) -> list:
    if catalog is None:
        return [html.Div("Aucun catalogue en mémoire.", className="stat")]

    entries = list(catalog.entries.values())
    clones = [e for e in entries if e.local is not None]
    on_disk = sum(e.local.size_bytes for e in clones)
    git_part = sum(e.local.git_dir_bytes for e in clones)
    free = catalog.reclaimable()
    freeable = sum(e.local.size_bytes for e in free)

    def stat(value: str, label: str, hero: bool = False) -> html.Div:
        return html.Div([html.Span(value, className="value"),
                         html.Span(label, className="label")],
                        className="stat hero" if hero else "stat")

    remote = [e for e in entries if e.remote is not None]
    cells = [
        stat(human(on_disk), "de clones", hero=True),
        stat(str(len(clones)), "clonés"),
        stat(str(len(remote)), "vus sur les forges"),
        stat(human(git_part), "d'historique .git"),
        stat(human(freeable), f"libérables ({len(free)})"),
    ]
    if not catalog.queried_hosts:
        # Sans cette précision, « 0 vus sur les forges » se lit comme « vous
        # n'avez aucun dépôt distant », ce qui est faux : rien n'a été demandé.
        cells.append(html.Div(
            "Aucune forge interrogée — « Connecter une forge » pour voir aussi "
            "les dépôts non clonés.",
            className="stat", style={"color": "var(--warning)"}))
    return cells


# ------------------------------------------------------------- disposition --

def layout() -> html.Div:
    return html.Div(id="repos-section", style={"display": "none", "flex": "1 1 auto",
                                               "minHeight": 0, "flexDirection": "column"},
                    children=[
        html.Div(className="toolbar", children=[
            html.Span("Lab :", style={"whiteSpace": "nowrap"}),
            dcc.Input(id="repos-domain", className="path-input", type="text",
                      placeholder="mon-lab.exemple.fr — adresse d'oauth-hub",
                      value=load_config().lab_domain or "", debounce=True,
                      style={"maxWidth": "320px"}),
            dcc.Input(id="repos-client", className="path-input", type="text",
                      placeholder="client Keycloak",
                      value=load_config().hub_client_id, debounce=True,
                      style={"maxWidth": "180px"}),
            html.Button("Enregistrer", id="repos-save", className="btn", n_clicks=0),
            html.Div(id="repos-endpoints", className="crumb"),
        ]),
        html.Div(id="repos-summary", className="summary", children=_summary(None)),
        html.Div(className="body", children=[
            html.Div(className="left", children=[
                # Deux barres plutôt qu'une : huit contrôles sur une seule ligne
                # se serrent jusqu'à devenir illisibles dans un panneau étroit.
                html.Div(className="toolbar", children=[
                    html.Button("Rafraîchir", id="repos-refresh", className="btn", n_clicks=0),
                    dcc.Checklist(
                        id="repos-offline", inline=True,
                        options=[{"label": " sans interroger les forges", "value": "off"}],
                        value=[], style={"fontSize": "12.5px"},
                    ),
                    html.Span("·", style={"opacity": .4}),
                    dcc.Dropdown(
                        id="repos-provider", clearable=False,
                        style={"width": "190px", "flex": "0 0 auto"},
                        options=[{"label": "GitHub", "value": "github"}], value="github",
                    ),
                    html.Button("Connecter", id="repos-login", className="btn", n_clicks=0),
                    html.Button("Déconnecter", id="repos-logout", className="btn ghost",
                                n_clicks=0),
                    html.Div(id="repos-identity", style={"marginLeft": "auto",
                                                         "whiteSpace": "nowrap"}),
                ]),
                html.Div(className="toolbar", children=[
                    html.Span("Forge :", style={"whiteSpace": "nowrap"}),
                    dcc.Dropdown(
                        id="repos-source", clearable=False,
                        style={"width": "200px", "flex": "0 0 auto"},
                        options=[{"label": "Toutes les forges", "value": "*"}], value="*",
                    ),
                    html.Span("Afficher :", style={"whiteSpace": "nowrap"}),
                    dcc.Dropdown(
                        id="repos-filter", clearable=False,
                        style={"width": "200px", "flex": "0 0 auto"},
                        options=[{"label": v, "value": k} for k, v in FILTERS.items()],
                        value="all",
                    ),
                    html.Div(className="tabs", style={"marginLeft": "auto"}, children=[
                        html.Button("Tailles", id="repos-tab-size", className="tab active", n_clicks=0),
                        html.Button("Submodules", id="repos-tab-graph", className="tab", n_clicks=0),
                    ]),
                ]),
                html.Div(className="graph-wrap", children=[
                    dcc.Graph(id="repos-chart", style={"height": "100%"},
                              config={"displaylogo": False, "responsive": True}),
                ]),
            ]),
            html.Div(className="right", children=[
                html.Div(className="toolbar", children=[
                    html.Span("Cochez un dépôt pour supprimer son clone local"),
                ]),
                html.Div(className="table-wrap", children=[
                    dash_table.DataTable(
                        id="repos-table",
                        columns=[
                            {"name": "Dépôt", "id": "nom"},
                            {"name": "État", "id": "etat"},
                            {"name": "Taille", "id": "taille"},
                            {"name": ".git", "id": "git"},
                            {"name": "Activité", "id": "activite"},
                            {"name": "Sur la forge", "id": "distant"},
                            {"name": "Ce qui retient", "id": "motif"},
                        ],
                        data=[], row_selectable="multi", selected_rows=[],
                        sort_action="native", page_size=100,
                        style_as_list_view=True,
                        style_cell={"fontFamily": "Inter, Segoe UI, system-ui, sans-serif",
                                    "fontSize": "12.5px", "padding": "6px 8px",
                                    "textAlign": "left", "border": "none",
                                    "maxWidth": 260, "overflow": "hidden",
                                    "textOverflow": "ellipsis"},
                        style_cell_conditional=[
                            {"if": {"column_id": c}, "textAlign": "right",
                             "fontVariantNumeric": "tabular-nums"} for c in ("taille", "git")
                        ],
                        style_data_conditional=[
                            {"if": {"filter_query": "{libre} = 1"},
                             "fontWeight": "600"},
                        ],
                        style_header={"fontWeight": "600", "border": "none",
                                      "borderBottom": "1px solid var(--border)"},
                    ),
                ]),
                html.Div(className="cleanup", children=[
                    html.Div(id="repos-selection", className="selection",
                             children="Rien de sélectionné."),
                    dcc.Checklist(
                        id="repos-force", inline=True,
                        options=[{"label": " forcer malgré les garde-fous", "value": "force"}],
                        value=[], style={"fontSize": "12.5px"},
                    ),
                    html.Button("Supprimer le clone local", id="repos-trash",
                                className="btn danger", n_clicks=0, disabled=True),
                    html.Button("Archiver", id="repos-archive", className="btn",
                                n_clicks=0, disabled=True),
                    html.Button("Désarchiver", id="repos-unarchive", className="btn",
                                n_clicks=0, disabled=True),
                    html.Button("Supprimer le dépôt distant", id="repos-rm-remote",
                                className="btn danger", n_clicks=0, disabled=True),
                ]),
                html.Div(id="repos-danger", className="cleanup", style={"display": "none"},
                         children=[
                    html.Div(id="repos-danger-text", className="selection"),
                    dcc.Input(id="repos-danger-input", className="path-input", type="text",
                              placeholder="chemin complet", value="",
                              style={"maxWidth": "300px"}),
                    html.Button("Confirmer la suppression", id="repos-danger-go",
                                className="btn danger", n_clicks=0, disabled=True),
                    html.Button("Annuler", id="repos-danger-cancel", className="btn ghost",
                                n_clicks=0),
                ]),
            ]),
        ]),
        dcc.ConfirmDialog(id="repos-confirm"),
        dcc.Store(id="repos-tab", data="size"),
        dcc.Store(id="repos-version", data=0),
        dcc.Interval(id="repos-poll", interval=500, disabled=True),
    ])


# -------------------------------------------------------------- callbacks --

def register(app) -> None:

    @app.callback(
        Output("repos-poll", "disabled"),
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Input("repos-refresh", "n_clicks"), State("repos-offline", "value"),
        prevent_initial_call=True,
    )
    def start(_clicks, offline):
        roots = load_config().repo_roots
        if not roots:
            return True, ("Aucune racine de recherche : renseignez REPO_ROOTS dans .env "
                          r"(ex. REPO_ROOTS=D:\github;D:\gitlab)"), "notice warn"
        _STATE["job"] = _CatalogJob(roots, "off" in (offline or []))
        return False, f"Lecture des dépôts sous {', '.join(roots)}…", "notice"

    @app.callback(
        Output("repos-poll", "disabled", allow_duplicate=True),
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("repos-version", "data", allow_duplicate=True),
        Input("repos-poll", "n_intervals"), State("repos-version", "data"),
        prevent_initial_call=True,
    )
    def watch(_ticks, version):
        job: _CatalogJob | None = _STATE.get("job")
        if job is None:
            return True, no_update, no_update, no_update
        if job.running:
            progress = f" ({job.done}/{job.total})" if job.total else ""
            return False, f"{job.step}{progress}", "notice", no_update

        _STATE["job"] = None
        if job.error:
            return True, job.error, "notice err", no_update

        if job.kind == "login":
            # Enchaîner sur un catalogue : c'est ce que l'utilisateur attend
            # après s'être connecté, et les forges sont maintenant joignables.
            _STATE["job"] = _CatalogJob(load_config().repo_roots, offline=False)
            return False, f"{job.message} — reconstruction du catalogue…", "notice ok", no_update

        _STATE["catalog"] = job.catalog
        count = len(job.catalog) if job.catalog else 0
        remote = sum(1 for e in job.catalog.entries.values() if e.remote) if job.catalog else 0
        return (True, f"{count} dépôt(s) au catalogue, dont {remote} vu(s) sur les forges.",
                "notice ok", (version or 0) + 1)

    @app.callback(
        Output("repos-endpoints", "children"),
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Input("repos-save", "n_clicks"), Input("mode", "data"),
        State("repos-domain", "value"), State("repos-client", "value"),
        prevent_initial_call=True,
    )
    def save_settings(_clicks, _mode, domain, client):
        from ..env import set_env_values
        from ..forge.config import reset_token_cache

        saving = callback_context.triggered_id == "repos-save"
        if saving:
            set_env_values({
                "LAB_DOMAIN": (domain or "").strip().rstrip("/"),
                "HUB_CLIENT_ID": (client or "storage-analysis").strip(),
            }, comment="Adresse du lab - oauth-hub y est joignable.")
            reset_token_cache()

        config = load_config()
        hub_config = config.hub()
        if hub_config is None:
            summary = "aucun lab configuré — l'adresse du gestionnaire oauth-hub est indispensable"
        else:
            # Afficher les URLs déduites : c'est la seule façon de vérifier d'un
            # coup d'oeil que le routage du lab correspond à ce qu'on suppose.
            summary = f"{hub_config.issuer}  ·  {hub_config.api_base}  ·  {hub_config.redirect_uri}"

        if not saving:
            return summary, no_update, no_update
        if hub_config is None:
            return summary, "Adresse effacée : plus aucun lab configuré.", "notice warn"
        return summary, f"Lab enregistré : {config.lab_domain}", "notice ok"

    @app.callback(
        Output("repos-poll", "disabled", allow_duplicate=True),
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Input("repos-login", "n_clicks"), State("repos-provider", "value"),
        prevent_initial_call=True,
    )
    def login(_clicks, slug):
        _STATE["job"] = _LoginJob(slug or "github")
        return False, (f"Connexion à « {slug} » — une page vient de s'ouvrir dans "
                       "votre navigateur, autorisez-y l'accès."), "notice"

    @app.callback(
        Output("repos-identity", "children"),
        Input("repos-provider", "options"), Input("repos-version", "data"),
    )
    def identity(options, _version):
        """Affiche sous quel compte l'application agit — jamais ambigu."""
        config = load_config()
        if not (config.lab_refresh_token or config.github_token):
            return html.Span("aucune session", style={"color": "var(--text-muted)"})
        relie = [o["label"] for o in (options or []) if "relié" in o.get("label", "")]
        if relie:
            return html.Span(relie[0], style={"color": "var(--good)"})
        source = "session de lab" if config.lab_refresh_token else "jeton local"
        return html.Span(source, style={"color": "var(--text-muted)"})

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("repos-version", "data", allow_duplicate=True),
        Input("repos-logout", "n_clicks"), State("repos-version", "data"),
        prevent_initial_call=True,
    )
    def logout(_clicks, version):
        from ..env import set_env_values
        from ..forge.config import reset_token_cache

        set_env_values({"GITHUB_TOKEN": "", "GITHUB_TOKEN_SCOPES": "",
                        "LAB_REFRESH_TOKEN": ""})
        reset_token_cache()
        _STATE["providers"] = None
        return ("Session effacée de ce poste. Le jeton reste valide côté forge tant "
                "qu'il n'est pas révoqué depuis oauth-hub.", "notice ok", (version or 0) + 1)

    @app.callback(
        Output("repos-source", "options"), Output("repos-source", "value"),
        Input("repos-version", "data"), Input("mode", "data"),
        State("repos-source", "value"),
    )
    def sources(_version, mode, current):
        # Deux sorties déclarées explicitement : avec une seule, Dash prendrait
        # la liste d'options renvoyée pour une liste de valeurs de sortie.
        options = _sources(_catalog())
        keep = current if any(o["value"] == current for o in options) else "*"
        return options, keep

    @app.callback(
        Output("repos-provider", "options"), Output("repos-provider", "value"),
        Input("mode", "data"), Input("repos-version", "data"),
        State("repos-provider", "value"),
    )
    def providers(mode, _version, current):
        """Le courtier fait autorité sur les sites disponibles."""
        fallback = [{"label": "GitHub", "value": "github"}]
        if mode != "repos":
            return no_update, no_update

        listing = _STATE.get("providers")
        if listing is None:
            config = load_config()
            hub_config = config.hub()
            listing = []
            if hub_config is not None and config.lab_refresh_token:
                from ..forge import hub
                try:
                    tokens = hub.refresh(hub_config, config.lab_refresh_token)
                    listing = hub.list_providers(hub_config, tokens.access_token)
                    # L'état de liaison n'est pas dans la liste : il vient de
                    # /status/, un appel par site — trois ou quatre au total.
                    for item in listing:
                        slug = item.get("slug")
                        if not slug:
                            continue
                        try:
                            item["_status"] = hub.status(hub_config, tokens.access_token, slug)
                        except hub.HubError:
                            item["_status"] = {}
                except hub.HubError:
                    listing = []               # sans session, on garde le repli
            _STATE["providers"] = listing

        if not listing:
            return fallback, current or "github"

        options = []
        for item in listing:
            slug = item.get("slug")
            if not slug or item.get("enabled") is False:
                continue
            label = item.get("display_name") or slug.capitalize()
            state = item.get("_status") or {}
            if state.get("connected"):
                account = state.get("account_label")
                label += f" · relié{f' ({account})' if account else ''}"
            elif not item.get("is_configured"):
                # Identifiants absents côté courtier : c'est un dev du lab qui
                # doit les déposer, pas l'utilisateur de cette application.
                label += " · à configurer dans oauth-hub"
            options.append({"label": label, "value": slug})

        if not options:
            return fallback, current or "github"
        keep = current if any(o["value"] == current for o in options) else options[0]["value"]
        return options, keep

    @app.callback(
        Output("repos-tab", "data"),
        Output("repos-tab-size", "className"), Output("repos-tab-graph", "className"),
        Input("repos-tab-size", "n_clicks"), Input("repos-tab-graph", "n_clicks"),
        prevent_initial_call=True,
    )
    def switch(_a, _b):
        chosen = "graph" if callback_context.triggered_id == "repos-tab-graph" else "size"
        return (chosen,
                "tab active" if chosen == "size" else "tab",
                "tab active" if chosen == "graph" else "tab")

    @app.callback(
        Output("repos-chart", "figure"), Output("repos-summary", "children"),
        Output("repos-table", "data"), Output("repos-table", "selected_rows"),
        Input("repos-version", "data"), Input("theme", "data"), Input("repos-tab", "data"),
        Input("mode", "data"), Input("repos-filter", "value"), Input("repos-source", "value"),
    )
    def render(_version, theme, tab, mode, kind, source):
        theme = theme or "light"
        catalog = _catalog()
        if catalog is None:
            return (figures.empty(theme, "Aucun catalogue — cliquez sur « Rafraîchir »."),
                    _summary(None), [], [])

        rows = _rows(catalog, kind or "all", source or "*")
        if not rows:
            where = "" if source in ("*", None) else f" sur {source}"
            return (figures.empty(theme,
                                  f"Aucun dépôt{where} : {FILTERS.get(kind, kind).lower()}."),
                    _summary(catalog), [], [])
        if tab == "graph":
            figure = figures.submodule_tree(_graph_nodes(catalog), theme)
        else:
            figure = figures.repos_bar(rows, theme)
        return figure, _summary(catalog), rows, []

    @app.callback(
        Output("repos-selection", "children"), Output("repos-trash", "disabled"),
        Input("repos-table", "selected_rows"), Input("repos-force", "value"),
        State("repos-table", "data"),
    )
    def selection(selected, force, data):
        if not selected or not data:
            return "Rien de sélectionné.", True
        chosen = [data[i] for i in selected if i < len(data)]
        clones = [row for row in chosen if row["chemin"]]
        blocked = [row for row in chosen if row["motif"]]
        total = sum(row["octets"] for row in clones)

        if not clones:
            return "Aucun clone local dans la sélection.", True

        parts = [f"{len(clones)} clone(s) — ", html.B(human(total)), " à récupérer"]
        if blocked:
            parts.append(f" · {len(blocked)} retenu(s) par un garde-fou")
        forced = "force" in (force or [])
        return html.Span(parts), bool(blocked) and not forced

    @app.callback(
        Output("repos-archive", "disabled"), Output("repos-unarchive", "disabled"),
        Output("repos-rm-remote", "disabled"),
        Input("repos-table", "selected_rows"), State("repos-table", "data"),
    )
    def remote_buttons(selected, data):
        """Les actions distantes n'ont de sens que sur des projets connus de la forge."""
        if not selected or not data:
            return True, True, True
        chosen = [data[i] for i in selected if i < len(data)]
        known = [r for r in chosen if r["distant"] not in ("—", "non interrogé")]
        if not known:
            return True, True, True
        archivable = any("archivé" not in r["distant"] for r in known)
        restorable = any("archivé" in r["distant"] for r in known)
        # Suppression distante : une seule entrée à la fois, pour que la
        # confirmation par saisie du chemin garde son sens.
        return not archivable, not restorable, len(known) != 1

    @app.callback(
        Output("repos-danger", "style"), Output("repos-danger-text", "children"),
        Output("repos-danger-input", "value"),
        Input("repos-rm-remote", "n_clicks"), Input("repos-danger-cancel", "n_clicks"),
        Input("repos-version", "data"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
        prevent_initial_call=True,
    )
    def danger_zone(_open, _cancel, _version, selected, data):
        hidden = {"display": "none"}
        if callback_context.triggered_id != "repos-rm-remote":
            return hidden, "", ""
        if not selected or not data:
            return hidden, "", ""

        row = data[selected[0]]
        catalog = _catalog()
        entry = catalog.entries.get(row["cle"]) if catalog else None
        if entry is None or entry.ref is None:
            return hidden, "", ""

        protege = actions.is_protected(entry, load_config().protected)
        message = [
            "Suppression IRRÉVERSIBLE de ", html.B(entry.key),
            " sur la forge. Saisissez ", html.B(entry.ref.path), " pour confirmer.",
        ]
        if protege:
            message += [html.Br(), f"Refusé : namespace protégé par FORGE_PROTECTED ({protege})."]
        elif entry.local is None:
            message += [html.Br(), "Aucun clone local : ce serait la dernière copie connue."]
        return {"display": "flex"}, html.Span(message), ""

    @app.callback(
        Output("repos-danger-go", "disabled"),
        Input("repos-danger-input", "value"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
    )
    def danger_ready(typed, selected, data):
        """Le bouton ne s'active qu'au chemin exact — jamais sur un simple clic."""
        if not selected or not data:
            return True
        catalog = _catalog()
        entry = catalog.entries.get(data[selected[0]]["cle"]) if catalog else None
        if entry is None or entry.ref is None:
            return True
        return (typed or "").strip() != entry.ref.path

    @app.callback(
        Output("repos-confirm", "displayed"), Output("repos-confirm", "message"),
        Input("repos-trash", "n_clicks"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
        State("repos-force", "value"),
        prevent_initial_call=True,
    )
    def ask(_clicks, selected, data, force):
        if not selected or not data:
            return False, ""
        chosen = [data[i] for i in selected if i < len(data) and data[i]["chemin"]]
        total = sum(row["octets"] for row in chosen)
        listing = "\n".join(f"  • {row['nom']}  ({row['taille']})" for row in chosen[:8])
        if len(chosen) > 8:
            listing += f"\n  … et {len(chosen) - 8} autre(s)"

        warning = ""
        blocked = [row for row in chosen if row["motif"]]
        if blocked and "force" in (force or []):
            warning = ("\n\nATTENTION — garde-fous ignorés :\n"
                       + "\n".join(f"  • {row['nom']} : {row['motif']}" for row in blocked[:5]))

        return True, (
            f"Envoyer {len(chosen)} clone(s) local(aux) à la corbeille ?\n"
            f"{human(total)} seront récupérés.\n\n{listing}{warning}\n\n"
            "Les dépôts distants ne sont pas touchés."
        )

    def _client_for(entry):
        """Client de la forge qui héberge cette entrée."""
        if entry.ref is None:
            return None
        for client in build_clients():
            if client.host == entry.ref.host:
                return client
        return None

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("repos-version", "data", allow_duplicate=True),
        Input("repos-archive", "n_clicks"), Input("repos-unarchive", "n_clicks"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
        State("repos-version", "data"),
        prevent_initial_call=True,
    )
    def archive(_a, _b, selected, data, version):
        catalog = _catalog()
        if catalog is None or not selected or not data:
            return no_update, no_update, no_update

        wanted = callback_context.triggered_id == "repos-archive"
        done, failures = 0, []
        for position in selected:
            if position >= len(data):
                continue
            entry = catalog.entries.get(data[position]["cle"])
            if entry is None or entry.remote is None:
                continue
            client = _client_for(entry)
            if client is None:
                failures.append(f"{entry.name} : aucun jeton pour {entry.ref.host}")
                continue
            outcome = actions.set_archived(entry, client, wanted)
            if outcome.ok:
                done += 1
            else:
                failures.append(f"{entry.name} : {outcome.message}")

        if done:
            cache.save(catalog, load_config().repo_roots)
            _STATE["catalog_stamp"] = None      # forcer la relecture au prochain rendu

        verb = "archivé(s)" if wanted else "désarchivé(s)"
        if not done:
            return "Aucun changement — " + " ; ".join(failures[:3]), "notice err", no_update
        message = f"{done} dépôt(s) {verb}"
        if failures:
            message += f" · {len(failures)} échec(s) : {'; '.join(failures[:2])}"
        return message, "notice warn" if failures else "notice ok", (version or 0) + 1

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("repos-version", "data", allow_duplicate=True),
        Input("repos-danger-go", "n_clicks"),
        State("repos-danger-input", "value"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
        State("repos-version", "data"),
        prevent_initial_call=True,
    )
    def delete_remote(_clicks, typed, selected, data, version):
        catalog = _catalog()
        if catalog is None or not selected or not data:
            return no_update, no_update, no_update

        entry = catalog.entries.get(data[selected[0]]["cle"])
        if entry is None:
            return no_update, no_update, no_update
        client = _client_for(entry)
        if client is None:
            return (f"Aucun jeton pour {entry.ref.host if entry.ref else '?'}",
                    "notice err", no_update)

        # Les garde-fous restent ceux de la ligne de commande : chemin saisi à
        # l'identique, namespace non protégé, clone local de secours exigé.
        outcome = actions.delete_remote(
            entry, client,
            confirmation=(typed or "").strip(),
            protected=load_config().protected,
            allow_last_copy=entry.local is None,
        )
        if not outcome.ok:
            return outcome.message, "notice err", no_update

        catalog.entries.pop(entry.key, None)
        cache.save(catalog, load_config().repo_roots)
        _STATE["catalog_stamp"] = None
        return outcome.message, "notice warn", (version or 0) + 1

    @app.callback(
        Output("notice", "children", allow_duplicate=True),
        Output("notice", "className", allow_duplicate=True),
        Output("repos-version", "data", allow_duplicate=True),
        Input("repos-confirm", "submit_n_clicks"),
        State("repos-table", "selected_rows"), State("repos-table", "data"),
        State("repos-force", "value"), State("repos-version", "data"),
        prevent_initial_call=True,
    )
    def do_delete(_submit, selected, data, force, version):
        catalog = _catalog()
        if catalog is None or not selected or not data:
            return no_update, no_update, no_update

        forced = "force" in (force or [])
        freed, done, failures = 0, 0, []
        for position in selected:
            if position >= len(data):
                continue
            entry = catalog.entries.get(data[position]["cle"])
            if entry is None or entry.local is None:
                continue
            outcome = actions.delete_local(entry, catalog, force=forced)
            if outcome.ok:
                freed += outcome.freed_bytes
                done += 1
                catalog.entries.pop(entry.key, None)
            else:
                failures.append(f"{entry.name} : {outcome.message}")

        if done:
            cache.save(catalog, load_config().repo_roots)

        if not done:
            return "Aucune suppression — " + " ; ".join(failures[:3]), "notice err", no_update
        message = f"{done} clone(s) à la corbeille — {human(freed)} récupérés"
        if failures:
            message += f" · {len(failures)} refus : {'; '.join(failures[:2])}"
        return message, "notice warn" if failures else "notice ok", (version or 0) + 1
