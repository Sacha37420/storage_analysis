"""Élagage adaptatif — la règle sans laquelle aucune vue ne tient.

Un disque réel porte des centaines de milliers d'entrées. Les envoyer telles
quelles à un moteur de rendu produit deux échecs simultanés : le navigateur
s'effondre, et l'écran devient une poussière de rectangles d'un pixel que
personne ne peut lire ni cliquer.

La parade, empruntée à DaisyDisk : sous chaque nœud, ne garder que les enfants
qui portent la masse — jusqu'à `keep_share` du cumul, ou `max_children` au plus —
et replier tout le reste dans un nœud de synthèse « … 1 243 autres éléments ».
L'information n'est pas perdue : elle est agrégée, visible, et son poids reste
exact. Le total d'un parent est donc toujours égal à la somme de ses enfants
affichés, ce dont Plotly a besoin en `branchvalues="total"`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .tree import FileTree

AGGREGATE_PREFIX = "agg:"


@dataclass(slots=True)
class PrunedView:
    """Arbre réduit, à plat, dans la forme attendue par Plotly."""

    ids: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    values: list[int] = field(default_factory=list)
    depths: list[int] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)   # -1 pour un nœud de synthèse
    paths: list[str] = field(default_factory=list)
    is_dir: list[bool] = field(default_factory=list)
    ages: list[float] = field(default_factory=list)    # jours depuis la modification
    counts: list[int] = field(default_factory=list)    # fichiers du sous-arbre
    truncated: int = 0                                 # éléments repliés

    def __len__(self) -> int:
        return len(self.ids)


def prune(
    tree: FileTree,
    root: int = 0,
    *,
    max_nodes: int = 2000,
    max_depth: int = 4,
    max_children: int = 40,
    keep_share: float = 0.95,
) -> PrunedView:
    """Réduit le sous-arbre de `root` à un ensemble affichable.

    `max_nodes` est un plafond dur : au-delà, la vue cesse d'être lisible bien
    avant de devenir lente.
    """
    view = PrunedView()
    total = tree.total_size
    files = tree.file_count
    now = time.time()

    def age_days(index: int) -> float:
        stamp = int(tree.mtime[index])
        return (now - stamp) / 86400.0 if stamp > 0 else -1.0

    def add(node_id: str, label: str, parent_id: str, value: int, depth: int,
            index: int, path: str, is_dir: bool, age: float, count: int) -> None:
        view.ids.append(node_id)
        view.labels.append(label)
        view.parents.append(parent_id)
        view.values.append(int(value))
        view.depths.append(depth)
        view.indices.append(index)
        view.paths.append(path)
        view.is_dir.append(is_dir)
        view.ages.append(age)
        view.counts.append(count)

    root_path = tree.path(root)
    add(str(root), tree.name(root) if root else root_path, "", int(total[root]), 0,
        root, root_path, True, age_days(root), int(files[root]))

    # Parcours en largeur : à budget épuisé, mieux vaut avoir dépensé les nœuds
    # sur les niveaux hauts, qui portent la décision, que sur une branche profonde.
    frontier: list[tuple[int, str, int]] = [(root, str(root), 0)]

    while frontier and len(view) < max_nodes:
        node, node_id, depth = frontier.pop(0)
        if depth >= max_depth:
            continue

        children = tree.children_by_size(node)
        if children.size == 0:
            continue

        parent_total = int(total[node])
        budget = max_nodes - len(view)
        kept: list[int] = []
        running = 0

        for child in children.tolist():
            if len(kept) >= max_children or len(kept) >= budget:
                break
            if kept and parent_total and running / parent_total >= keep_share:
                break
            kept.append(child)
            running += int(total[child])

        for child in kept:
            child_id = str(child)
            add(
                child_id, tree.name(child), node_id, int(total[child]), depth + 1,
                child, tree.path(child), tree.is_dir(child), age_days(child),
                int(files[child]),
            )
            if tree.is_dir(child):
                frontier.append((child, child_id, depth + 1))

        # Le reliquat : jamais silencieux, toujours pesé.
        remainder = parent_total - running
        hidden = int(children.size) - len(kept)
        if hidden > 0 and remainder > 0:
            view.truncated += hidden
            add(
                f"{AGGREGATE_PREFIX}{node}", f"… {hidden} autres éléments", node_id,
                remainder, depth + 1, -1, tree.path(node), False, -1.0, 0,
            )

    return view


def color_values(view: PrunedView, mode: str) -> tuple[list[float], str, float, float]:
    """Valeurs continues pour la couleur, avec leur légende et leurs bornes.

    Deux encodages, tous deux séquentiels (une seule teinte, clair → foncé) :
    la profondeur pour lire la structure, l'ancienneté pour repérer le froid.
    """
    if mode == "age":
        ages = [a for a in view.ages if a >= 0]
        top = max(ages) if ages else 1.0
        # Un dossier récemment touché n'informe pas : c'est le vieux qui compte.
        values = [a if a >= 0 else 0.0 for a in view.ages]
        return values, "Ancienneté (jours)", 0.0, max(top, 1.0)

    depths = [float(d) for d in view.depths]
    return depths, "Profondeur", 0.0, max(max(depths, default=1.0), 1.0)
