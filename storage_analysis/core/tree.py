"""Modèle d'arbre compact en tableaux parallèles.

Un objet Python par fichier coûte ~500 octets, soit plusieurs Go pour un disque
d'un million de fichiers. Ici chaque entrée est un simple indice entier dans des
tableaux NumPy alignés, pour ~35-40 octets par entrée :

    parent[i]    int32   indice du dossier parent (0 pour la racine, qui est son
                         propre parent — l'agrégation ignore le niveau 0)
    size[i]      int64   taille propre (0 pour les dossiers)
    mtime[i]     int64   date de modification, epoch en secondes
    depth[i]     int16   profondeur, 0 = racine
    flags[i]     uint8   FLAG_DIR | FLAG_LINK | FLAG_ERROR
    name_off[i]  int64   bornes du nom dans name_blob (n+1 valeurs)

Les noms vivent dans un unique blob UTF-8 plutôt que dans une liste de str.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import numpy as np

FLAG_DIR = 0x01
FLAG_LINK = 0x02
FLAG_ERROR = 0x04

# surrogatepass : les noms de fichiers Windows peuvent contenir des demi-paires
# UTF-16 invalides ; on les fait circuler sans perte plutôt que de planter.
_ENCODING = "utf-8"
_ERRORS = "surrogatepass"


class FileTree:
    """Arborescence scannée, agrégée à la demande."""

    __slots__ = (
        "parent", "size", "mtime", "depth", "flags", "name_off", "name_blob",
        "meta", "_total", "_files", "_dirs", "_child_start", "_child_index",
        "_hidden",
    )

    def __init__(
        self,
        parent: np.ndarray,
        size: np.ndarray,
        mtime: np.ndarray,
        depth: np.ndarray,
        flags: np.ndarray,
        name_off: np.ndarray,
        name_blob: bytes,
        meta: dict[str, Any] | None = None,
    ) -> None:
        n = len(parent)
        assert len(size) == len(mtime) == len(depth) == len(flags) == n
        assert len(name_off) == n + 1

        self.parent = parent
        self.size = size
        self.mtime = mtime
        self.depth = depth
        self.flags = flags
        self.name_off = name_off
        self.name_blob = name_blob
        self.meta: dict[str, Any] = meta or {}

        self._total: np.ndarray | None = None
        self._files: np.ndarray | None = None
        self._dirs: np.ndarray | None = None
        self._child_start: np.ndarray | None = None
        self._child_index: np.ndarray | None = None
        # Éléments supprimés pendant la session : ils sortent des totaux et des
        # listes d'enfants sans qu'il faille refaire un scan complet du disque.
        self._hidden: np.ndarray | None = None

    # ------------------------------------------------------- suppressions --

    @property
    def hidden(self) -> np.ndarray:
        if self._hidden is None:
            self._hidden = np.zeros(len(self), dtype=bool)
        return self._hidden

    def hide_subtree(self, i: int) -> int:
        """Retire un sous-arbre des vues. Renvoie l'espace ainsi libéré."""
        freed = int(self.total_size[i])
        self.hidden[self.subtree(i)] = True
        self._total = self._files = self._dirs = None  # agrégats à recalculer
        return freed

    # ------------------------------------------------------------ basiques --

    def __len__(self) -> int:
        return len(self.parent)

    def __repr__(self) -> str:
        return f"<FileTree {self.root_path!r} {len(self)} entrées>"

    @property
    def root_path(self) -> str:
        return self.name(0)

    def name(self, i: int) -> str:
        a, b = int(self.name_off[i]), int(self.name_off[i + 1])
        return self.name_blob[a:b].decode(_ENCODING, _ERRORS)

    def path(self, i: int) -> str:
        """Chemin absolu reconstruit en remontant les parents."""
        parts: list[str] = []
        node = int(i)
        while node > 0:
            parts.append(self.name(node))
            node = int(self.parent[node])
        parts.append(self.name(0))
        parts.reverse()
        return os.path.join(*parts)

    def is_dir(self, i: int) -> bool:
        return bool(self.flags[i] & FLAG_DIR)

    def is_link(self, i: int) -> bool:
        return bool(self.flags[i] & FLAG_LINK)

    # ---------------------------------------------------------- agrégation --

    def _aggregate(self) -> None:
        """Somme les tailles et les compteurs de bas en haut, niveau par niveau."""
        n = len(self)
        total = self.size.astype(np.int64)
        is_dir = (self.flags & FLAG_DIR).astype(bool)
        files = (~is_dir).astype(np.int64)
        dirs = is_dir.astype(np.int64)

        if self._hidden is not None and self._hidden.any():
            live = ~self._hidden
            total = total * live
            files = files * live
            dirs = dirs * live

        if n > 1:
            order = np.argsort(self.depth, kind="stable")
            sorted_depth = self.depth[order]
            max_depth = int(sorted_depth[-1])
            bounds = np.searchsorted(sorted_depth, np.arange(max_depth + 2))

            # Du niveau le plus profond vers 1 : chaque nœud verse son cumul à
            # son parent. Le niveau 0 (racine) n'a pas de parent à créditer.
            for level in range(max_depth, 0, -1):
                idx = order[bounds[level] : bounds[level + 1]]
                if idx.size == 0:
                    continue
                parents = self.parent[idx]
                np.add.at(total, parents, total[idx])
                np.add.at(files, parents, files[idx])
                np.add.at(dirs, parents, dirs[idx])

        self._total, self._files, self._dirs = total, files, dirs

    @property
    def total_size(self) -> np.ndarray:
        """Taille cumulée du sous-arbre de chaque nœud."""
        if self._total is None:
            self._aggregate()
        return self._total  # type: ignore[return-value]

    @property
    def file_count(self) -> np.ndarray:
        """Nombre de fichiers dans le sous-arbre de chaque nœud."""
        if self._files is None:
            self._aggregate()
        return self._files  # type: ignore[return-value]

    @property
    def dir_count(self) -> np.ndarray:
        """Nombre de dossiers du sous-arbre, nœud lui-même inclus."""
        if self._dirs is None:
            self._aggregate()
        return self._dirs  # type: ignore[return-value]

    # ---------------------------------------------------------- navigation --

    def _build_children(self) -> None:
        """Index CSR parent -> enfants, construit en un tri."""
        n = len(self)
        parents = self.parent[1:]  # la racine n'est l'enfant de personne
        order = np.argsort(parents, kind="stable") + 1
        counts = np.bincount(parents, minlength=n)
        start = np.zeros(n + 1, dtype=np.int64)
        np.cumsum(counts, out=start[1:])
        self._child_start, self._child_index = start, order

    def children(self, i: int) -> np.ndarray:
        """Indices des enfants directs, dans l'ordre du scan."""
        if self._child_start is None:
            self._build_children()
        start = self._child_start  # type: ignore[union-attr]
        return self._child_index[start[i] : start[i + 1]]  # type: ignore[index]

    def children_by_size(self, i: int, limit: int | None = None) -> np.ndarray:
        """Enfants directs triés par taille cumulée décroissante."""
        kids = self.children(i)
        if self._hidden is not None and self._hidden.any():
            kids = kids[~self._hidden[kids]]
        if kids.size == 0 or limit == 0:
            return kids[:0]
        order = np.argsort(self.total_size[kids], kind="stable")[::-1]
        ranked = kids[order]
        # limit None = tous ; limit <= 0 = aucun (et surtout pas « tous », piège
        # classique de la tranche [:0] / [-0:]).
        return ranked if limit is None else ranked[:max(0, limit)]

    def subtree(self, i: int) -> np.ndarray:
        """Indices de tout le sous-arbre de `i`, `i` inclus."""
        if i == 0:
            return np.arange(len(self), dtype=np.int64)
        out = [np.array([i], dtype=np.int64)]
        frontier = self.children(i)
        while frontier.size:
            out.append(frontier)
            if self._child_start is None:
                self._build_children()
            start = self._child_start  # type: ignore[union-attr]
            spans = [
                self._child_index[start[k] : start[k + 1]]  # type: ignore[index]
                for k in frontier
                if start[k + 1] > start[k]
            ]
            frontier = np.concatenate(spans) if spans else np.empty(0, dtype=np.int64)
        return np.concatenate(out)

    # ------------------------------------------------------------ requêtes --

    def largest_files(self, k: int = 20, under: int = 0) -> np.ndarray:
        """Les `k` plus gros fichiers du sous-arbre de `under`, du plus gros au plus petit."""
        if k <= 0:
            return np.empty(0, dtype=np.int64)
        scope = np.arange(len(self), dtype=np.int64) if under == 0 else self.subtree(under)
        files = scope[(self.flags[scope] & FLAG_DIR) == 0]
        if files.size == 0:
            return files
        if files.size > k:
            files = files[np.argpartition(self.size[files], -k)[-k:]]
        return files[np.argsort(self.size[files], kind="stable")[::-1]]

    def extension_totals(self, under: int = 0) -> list[tuple[str, int, int]]:
        """Répartition (extension, octets, nombre) triée par volume décroissant."""
        scope = np.arange(len(self), dtype=np.int64) if under == 0 else self.subtree(under)
        files = scope[(self.flags[scope] & FLAG_DIR) == 0]
        totals: dict[str, list[int]] = {}
        for i in files.tolist():
            ext = os.path.splitext(self.name(i))[1].lower() or "(sans extension)"
            slot = totals.setdefault(ext, [0, 0])
            slot[0] += int(self.size[i])
            slot[1] += 1
        return sorted(
            ((ext, v[0], v[1]) for ext, v in totals.items()),
            key=lambda row: row[1],
            reverse=True,
        )

    def walk_by_size(
        self, i: int = 0, max_depth: int = 2, top: int = 10, min_share: float = 0.01
    ) -> Iterator[tuple[int, int, bool]]:
        """Parcours des plus grosses branches : (indice, profondeur relative, dernier).

        Ne descend que dans les enfants qui pèsent au moins `min_share` du parent,
        et au plus `top` par niveau : de quoi afficher un arbre lisible.
        """
        total = self.total_size

        def _rec(node: int, level: int) -> Iterator[tuple[int, int, bool]]:
            if level >= max_depth:
                return
            parent_total = int(total[node]) or 1
            kids = [
                int(k)
                for k in self.children_by_size(node, limit=top)
                if int(total[k]) / parent_total >= min_share
            ]
            for pos, kid in enumerate(kids):
                yield kid, level, pos == len(kids) - 1
                if self.is_dir(kid):
                    yield from _rec(kid, level + 1)

        yield from _rec(int(i), 0)
