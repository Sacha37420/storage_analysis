"""Construction des figures Plotly.

Chaque forme est choisie pour le travail qu'elle fait, pas pour l'effet :

* **treemap squarifié** — repérer la masse d'un coup d'œil. Rapport d'aspect
  proche de 1, donc des surfaces réellement comparables ;
* **icicle** — lire la structure et la profondeur des chemins, libellés
  horizontaux donc lisibles, sans le biais de surface du sunburst ;
* **barres horizontales** — comparer des extensions, série unique, valeurs
  étiquetées directement.
"""

from __future__ import annotations

import plotly.graph_objects as go

from ..core.prune import PrunedView, color_values
from ..fmt import human
from .theme import axis, layout, palette, scale


def _colorscale(theme: str, mode: str) -> list[list]:
    steps = scale(theme, mode)
    last = len(steps) - 1
    return [[index / last, color] for index, color in enumerate(steps)]


def _age_label(days: float) -> str:
    if days < 0:
        return "—"
    if days < 1:
        return "aujourd'hui"
    if days < 31:
        return f"{int(days)} j"
    if days < 365:
        return f"{int(days / 30.44)} mois"
    return f"{days / 365.25:.0f} an(s)"


def _hierarchy_common(view: PrunedView, theme: str, mode: str) -> dict:
    colors = palette(theme)
    values, legend, low, high = color_values(view, mode)

    # Le libellé n'est affiché que s'il tient : uniformtext.mode="hide" côté
    # layout se charge de masquer plutôt que de rogner.
    text = [human(v) for v in view.values]
    customdata = [
        [path, _age_label(age), f"{count:,}".replace(",", " "), "dossier" if is_dir else "fichier"]
        for path, age, count, is_dir in zip(view.paths, view.ages, view.counts, view.is_dir)
    ]

    return {
        "ids": view.ids,
        "labels": view.labels,
        "parents": view.parents,
        "values": view.values,
        "branchvalues": "total",
        "text": text,
        "customdata": customdata,
        "hovertemplate": (
            "<b>%{label}</b><br>%{text}"
            "<br>%{customdata[3]} · %{customdata[2]} fichier(s)"
            "<br>modifié : %{customdata[1]}"
            "<br><span style='font-size:11px'>%{customdata[0]}</span>"
            "<extra></extra>"
        ),
        "marker": {
            "colors": values,
            "colorscale": _colorscale(theme, mode),
            "cmin": low,
            "cmax": high,
            # Le trait est de la couleur de la surface : c'est un espace entre
            # les pavés, pas une bordure dessinée autour d'eux.
            "line": {"width": 2, "color": colors["surface"]},
            "colorbar": {
                "title": {"text": legend, "font": {"size": 11, "color": colors["text_muted"]}},
                "thickness": 10,
                "len": 0.55,
                "y": 0.5,
                "outlinewidth": 0,
                "tickfont": {"size": 10, "color": colors["text_muted"]},
            },
        },
        "textfont": {"size": 12, "color": colors["text"]},
        "insidetextfont": {"size": 12},
        "outsidetextfont": {"size": 12, "color": colors["text_secondary"]},
        "pathbar": {
            "visible": True,
            "side": "top",
            "thickness": 22,
            "textfont": {"size": 12, "color": colors["text_secondary"]},
        },
    }


def treemap(view: PrunedView, theme: str = "light", mode: str = "depth") -> go.Figure:
    """Vue principale : surface ∝ taille, imbrication = hiérarchie."""
    common = _hierarchy_common(view, theme, mode)
    figure = go.Figure(go.Treemap(
        **common,
        tiling={"packing": "squarify", "pad": 2},
        textposition="middle center",
        textinfo="label+text",
        maxdepth=3,       # au-delà, l'écran devient illisible ; le clic creuse
    ))
    figure.update_layout(**layout(theme))
    return figure


def icicle(view: PrunedView, theme: str = "light", mode: str = "depth") -> go.Figure:
    """Vue structure : une ligne par niveau, libellés horizontaux."""
    common = _hierarchy_common(view, theme, mode)
    figure = go.Figure(go.Icicle(
        **common,
        # orientation « v » : racine en haut, cascade vers le bas. Les libellés
        # restent horizontaux — c'est tout l'intérêt face au sunburst.
        tiling={"orientation": "v", "pad": 2},
        textposition="middle center",
        textinfo="label+text",
        maxdepth=5,
    ))
    figure.update_layout(**layout(theme))
    return figure


def extensions_bar(rows: list[tuple[str, int, int]], theme: str, limit: int = 14) -> go.Figure:
    """Répartition par extension. Série unique : ni légende, ni couleur porteuse."""
    colors = palette(theme)
    rows = rows[:limit][::-1]     # Plotly empile de bas en haut

    if not rows:
        return go.Figure(layout=layout(theme))

    labels = [name for name, _, _ in rows]
    sizes = [size for _, size, _ in rows]
    counts = [count for _, _, count in rows]

    figure = go.Figure(go.Bar(
        x=sizes,
        y=labels,
        orientation="h",
        marker={"color": colors["series"]},
        text=[human(size) for size in sizes],
        textposition="outside",
        textfont={"size": 11, "color": colors["text_secondary"]},
        customdata=[[f"{c:,}".replace(",", " ")] for c in counts],
        hovertemplate="<b>%{y}</b><br>%{text} · %{customdata[0]} fichiers<extra></extra>",
        cliponaxis=False,
    ))
    figure.update_layout(
        **layout(
            theme,
            barcornerradius=4,          # extrémités arrondies, ancrées à la ligne de base
            margin={"l": 8, "r": 64, "t": 8, "b": 28},
            bargap=0.35,
            showlegend=False,
        )
    )
    figure.update_xaxes(**axis(theme, showgrid=True, title=None, tickformat="~s", ticksuffix="o"))
    figure.update_yaxes(**axis(theme, showgrid=False, title=None,
                               tickfont={"size": 12, "color": colors["text_secondary"]}))
    return figure


def repos_bar(rows: list[dict], theme: str, limit: int = 18) -> go.Figure:
    """Dépôts par taille, en mettant en avant ce qui est supprimable.

    Deux classes seulement, donc légende obligatoire et couleur qui porte
    vraiment une décision : ce qui peut partir contre ce qui est retenu. Le
    reste est estompé — c'est le motif « mettre en avant, estomper » plutôt
    qu'un arc-en-ciel de dépôts.
    """
    colors = palette(theme)
    rows = [r for r in rows if r.get("octets")][:limit][::-1]
    if not rows:
        return empty(theme, "Aucun dépôt à afficher.")

    figure = go.Figure()
    order = [r["nom"] for r in rows]

    for label, color, wanted in (
        ("Supprimable sans perte", colors["series"], True),
        ("Retenu", colors["muted_mark"], False),
    ):
        subset = [r for r in rows if bool(r["libre"]) is wanted]
        if not subset:
            continue
        figure.add_bar(
            x=[r["octets"] for r in subset],
            y=[r["nom"] for r in subset],
            name=label,
            orientation="h",
            marker={"color": color},
            text=[r["taille"] for r in subset],
            textposition="outside",
            textfont={"size": 11, "color": colors["text_secondary"]},
            customdata=[[r["cle"], r["motif"] or "—"] for r in subset],
            hovertemplate=("<b>%{y}</b><br>%{text}"
                           "<br>%{customdata[0]}"
                           "<br>%{customdata[1]}<extra></extra>"),
            cliponaxis=False,
        )

    figure.update_layout(**layout(
        theme,
        barcornerradius=4,
        margin={"l": 8, "r": 72, "t": 8, "b": 28},
        bargap=0.35,
        legend={"orientation": "h", "y": 1.06, "x": 0,
                "font": {"size": 11, "color": colors["text_secondary"]}},
    ))
    figure.update_xaxes(**axis(theme, title=None, tickformat="~s", ticksuffix="o"))
    figure.update_yaxes(**axis(theme, showgrid=False, title=None,
                               categoryorder="array", categoryarray=order,
                               tickfont={"size": 12, "color": colors["text_secondary"]}))
    return figure


def submodule_tree(nodes: list[dict], theme: str) -> go.Figure:
    """Liens entre dépôts : qui embarque qui, en pavage hiérarchique.

    La taille du secteur est celle du clone, pour que le poids réel du montage
    apparaisse en même temps que sa structure.
    """
    colors = palette(theme)
    if not nodes:
        return empty(theme, "Aucun .gitmodules : les dépôts sont indépendants.")

    figure = go.Figure(go.Icicle(
        ids=[n["id"] for n in nodes],
        labels=[n["label"] for n in nodes],
        parents=[n["parent"] for n in nodes],
        values=[n["value"] for n in nodes],
        text=[n["note"] for n in nodes],
        marker={
            "colors": [n["depth"] for n in nodes],
            "colorscale": _colorscale(theme, "depth"),
            "showscale": False,
            "line": {"width": 2, "color": colors["surface"]},
        },
        tiling={"orientation": "v", "pad": 2},
        textposition="middle center",
        textinfo="label+text",
        hovertemplate="<b>%{label}</b><br>%{text}<extra></extra>",
    ))
    figure.update_layout(**layout(theme))
    return figure


def empty(theme: str, message: str) -> go.Figure:
    """Espace réservé lisible, plutôt qu'un cadre vide qui laisse croire à un bug."""
    colors = palette(theme)
    figure = go.Figure()
    figure.add_annotation(
        text=message, showarrow=False,
        font={"family": "Inter, Segoe UI, system-ui, sans-serif",
              "size": 14, "color": colors["text_muted"]},
    )
    figure.update_layout(**layout(theme))
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    return figure
