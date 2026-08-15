"""Union des clones locaux et des projets distants, et graphe des submodules.

Le catalogue est la structure que consomment l'affichage et les actions. Il
répond à trois questions :

  * qu'est-ce qui existe, où (disque, forge, les deux) ?
  * quels dépôts dépendent de quels autres via des submodules ?
  * que puis-je supprimer sans rien perdre ?
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .models import CatalogEntry, LocalRepo, RemoteRepo, RepoRef


@dataclass(slots=True)
class SubmoduleEdge:
    """Un lien « le dépôt parent inclut le dépôt enfant »."""

    parent_key: str
    rel_path: str
    url: str
    child_ref: RepoRef | None       # None si l'URL n'est pas reconnue
    child_key: str | None           # clé dans le catalogue, si elle y figure
    child_known: bool               # l'enfant est-il présent au catalogue ?
    checked_out: bool = False       # le submodule est-il déployé sur le disque ?


@dataclass(slots=True)
class Catalog:
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    edges: list[SubmoduleEdge] = field(default_factory=list)
    # Hôtes réellement interrogés via l'API. Sans cette information, l'absence
    # de projet distant serait lue à tort comme « le projet n'existe plus ».
    queried_hosts: set[str] = field(default_factory=set)

    # ------------------------------------------------------------ accès --

    def __len__(self) -> int:
        return len(self.entries)

    def sorted_entries(self, key: str = "size") -> list[CatalogEntry]:
        values = list(self.entries.values())
        if key == "size":
            return sorted(values, key=lambda e: e.size_bytes, reverse=True)
        if key == "activity":
            return sorted(values, key=lambda e: e.last_activity or 0, reverse=True)
        return sorted(values, key=lambda e: e.key)

    def local_entries(self) -> list[CatalogEntry]:
        return [e for e in self.entries.values() if e.local is not None]

    # -------------------------------------------------------- submodules --

    def children_of(self, key: str) -> list[SubmoduleEdge]:
        return [edge for edge in self.edges if edge.parent_key == key]

    def parents_of(self, key: str) -> list[SubmoduleEdge]:
        return [edge for edge in self.edges if edge.child_key == key]

    def roots(self) -> list[str]:
        """Dépôts qui incluent des submodules sans être eux-mêmes inclus."""
        children = {edge.child_key for edge in self.edges if edge.child_key}
        parents = {edge.parent_key for edge in self.edges}
        return sorted(parents - children)

    # ------------------------------------------------------------ sûreté --

    def blockers(self, entry: CatalogEntry) -> list[str]:
        """Raisons de ne pas supprimer le clone local. Vide = suppression sûre.

        On refuse par défaut dès qu'un de ces points est vrai : c'est
        exactement ce qui distingue « je récupère de la place » de
        « je perds du travail ».
        """
        reasons: list[str] = []
        local = entry.local
        if local is None:
            return ["aucun clone local"]

        if local.ref is None:
            reasons.append("aucun remote : ce dépôt n'existe nulle part ailleurs")
        elif entry.remote is None and local.ref.host in self.queried_hosts:
            reasons.append("remote déclaré mais projet introuvable sur la forge")
        elif entry.remote is None:
            reasons.append(f"forge {local.ref.host} non interrogée (jeton absent)")
        if local.ahead > 0:
            reasons.append(f"{local.ahead} commit(s) non poussé(s)")
        if local.dirty > 0:
            reasons.append(f"{local.dirty} fichier(s) modifié(s) ou non suivi(s)")

        used_by = [
            edge.parent_key
            for edge in self.parents_of(entry.key)
            if edge.checked_out or edge.parent_key in self.entries
        ]
        if used_by:
            reasons.append("inclus comme submodule par " + ", ".join(sorted(set(used_by))))

        return reasons

    def reclaimable(self) -> list[CatalogEntry]:
        """Clones locaux supprimables sans perte, du plus gros au plus petit."""
        safe = [e for e in self.local_entries() if not self.blockers(e)]
        return sorted(safe, key=lambda e: e.size_bytes, reverse=True)


# ------------------------------------------------------------ construction --

def build_catalog(
    local_repos: list[LocalRepo] | None = None,
    remote_repos: list[RemoteRepo] | None = None,
    queried_hosts: set[str] | None = None,
) -> Catalog:
    """Fusionne les deux sources sur la clé normalisée hôte/chemin."""
    catalog = Catalog(queried_hosts=set(queried_hosts or ()))
    local_repos = local_repos or []
    remote_repos = remote_repos or []

    for repo in local_repos:
        if repo.ref is not None:
            key = repo.ref.key
            existing = catalog.entries.get(key)
            if existing is not None and existing.local is not None:
                # Deux clones du même projet : on garde le plus gros et on
                # signale le doublon, qui est justement une piste de ménage.
                if repo.size_bytes <= existing.local.size_bytes:
                    continue
            catalog.entries[key] = CatalogEntry(ref=repo.ref, local=repo)
        else:
            entry = CatalogEntry(ref=None, local=repo)
            catalog.entries[entry.key] = entry

    for project in remote_repos:
        key = project.ref.key
        entry = catalog.entries.get(key)
        if entry is None:
            catalog.entries[key] = CatalogEntry(ref=project.ref, remote=project)
        else:
            entry.remote = project

    catalog.edges = _build_edges(catalog)
    return catalog


def _build_edges(catalog: Catalog) -> list[SubmoduleEdge]:
    import os

    edges: list[SubmoduleEdge] = []
    for entry in catalog.entries.values():
        for declaration in entry.submodules:
            child_key = declaration.ref.key if declaration.ref else None
            known = child_key is not None and child_key in catalog.entries

            checked_out = False
            if entry.local is not None and declaration.rel_path:
                candidate = os.path.join(entry.local.path, declaration.rel_path)
                checked_out = os.path.exists(os.path.join(candidate, ".git"))

            edges.append(
                SubmoduleEdge(
                    parent_key=entry.key,
                    rel_path=declaration.rel_path,
                    url=declaration.url,
                    child_ref=declaration.ref,
                    child_key=child_key,
                    child_known=known,
                    checked_out=checked_out,
                )
            )
    return edges


def match_entries(catalog: Catalog, pattern: str) -> list[CatalogEntry]:
    """Sélectionne des entrées par motif : nom exact, chemin, ou glob."""
    pattern_lower = pattern.lower()
    exact: list[CatalogEntry] = []
    globbed: list[CatalogEntry] = []

    for entry in catalog.entries.values():
        candidates = {entry.key.lower(), entry.name.lower()}
        if entry.ref is not None:
            candidates.add(entry.ref.path.lower())
        if entry.local is not None:
            candidates.add(entry.local.path.lower())

        if pattern_lower in candidates:
            exact.append(entry)
        elif any(fnmatch.fnmatch(value, pattern_lower) for value in candidates):
            globbed.append(entry)

    return exact or globbed
