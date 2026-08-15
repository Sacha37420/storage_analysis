"""Palette et gabarits Plotly, en clair et en sombre.

Les deux modes sont **choisis**, pas dérivés l'un de l'autre par inversion : les
pas sombres sont pris dans les mêmes rampes mais calés sur la surface sombre.

Encodage des couleurs, décidé selon le travail que fait chaque graphique :

* **treemap et icicle** — la question est « où est la masse et comment est-ce
  structuré ». C'est de la magnitude, donc une rampe **séquentielle d'une seule
  teinte**. Pas huit couleurs par type de fichier : dans un pavage, n'importe
  quels deux rectangles peuvent devenir voisins, et un jeu catégoriel ne tient
  les seuils de séparation que sur trois teintes — trop peu pour être utile.
  L'identité des types vit dans le graphe d'extensions et dans la table.
* **extensions** — série unique, donc pas de légende : la teinte ne porte
  aucune information, les libellés sont directs.
"""

from __future__ import annotations

# --------------------------------------------------------------- palettes --

LIGHT = {
    "surface": "#fcfcfb",
    "surface_alt": "#f4f3f0",
    "border": "#e2e1dc",
    "text": "#0b0b0b",
    "text_secondary": "#52514e",
    "text_muted": "#78766f",
    "accent": "#2a78d6",
    "series": "#2a78d6",
    # Rampe séquentielle bleue, 100 → 700
    "seq": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
    # Second contexte séquentiel (ancienneté) : la teinte du slot suivant
    "seq_alt": ["#fbe3d3", "#f6c3a4", "#f0a074", "#eb6834", "#c9521f", "#a03f16", "#732c0f"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "critical": "#d03b3b",
    # Marque atténuée : sert au motif « mettre en avant, estomper le reste »,
    # quand une seule classe porte le message.
    "muted_mark": "#c9c7c0",
}

DARK = {
    "surface": "#1a1a19",
    "surface_alt": "#222221",
    "border": "#383835",
    "text": "#ffffff",
    "text_secondary": "#c3c2b7",
    "text_muted": "#8f8d84",
    "accent": "#3987e5",
    "series": "#3987e5",
    "seq": ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"],
    "seq_alt": ["#732c0f", "#a03f16", "#c9521f", "#d95926", "#f0a074", "#f6c3a4", "#fbe3d3"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "critical": "#d03b3b",
    "muted_mark": "#55544f",
}


def palette(theme: str) -> dict:
    return DARK if theme == "dark" else LIGHT


def scale(theme: str, mode: str = "depth") -> list[str]:
    """Rampe séquentielle : bleue pour la profondeur, orange pour l'ancienneté."""
    colors = palette(theme)
    return colors["seq_alt"] if mode == "age" else colors["seq"]


# ---------------------------------------------------------------- gabarit --

FONT = "Inter, Segoe UI, system-ui, sans-serif"


def layout(theme: str, **overrides) -> dict:
    """Mise en page commune : surfaces neutres, chrome discret, pas de grille inutile."""
    colors = palette(theme)
    base = {
        "paper_bgcolor": colors["surface"],
        "plot_bgcolor": colors["surface"],
        "font": {"family": FONT, "size": 13, "color": colors["text_secondary"]},
        "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
        "hoverlabel": {
            "bgcolor": colors["surface_alt"],
            "bordercolor": colors["border"],
            "font": {"family": FONT, "size": 12, "color": colors["text"]},
            "align": "left",
        },
        "separators": ", ",     # 1 234,5 — convention française
        "uniformtext": {"minsize": 11, "mode": "hide"},   # jamais de libellé rogné
    }
    base.update(overrides)
    return base


def axis(theme: str, **overrides) -> dict:
    """Axe en filet, une nuance au-dessus de la surface. Jamais de pointillés."""
    colors = palette(theme)
    base = {
        "showgrid": True,
        "gridcolor": colors["border"],
        "gridwidth": 1,
        "zeroline": False,
        "linecolor": colors["border"],
        "tickfont": {"size": 11, "color": colors["text_muted"]},
        "title": {"font": {"size": 12, "color": colors["text_muted"]}},
    }
    base.update(overrides)
    return base
