"""Vues texte du catalogue de dépôts.

Trois angles, comme pour l'analyse disque : une liste pour agir, un graphe pour
comprendre les liens, un palmarès pour décider quoi supprimer.
"""

from __future__ import annotations

import sys

from ..fmt import ARROW, DASH, ELBOW, PIPE, TEE, UP, count, human, since
from .catalog import Catalog
from .models import CatalogEntry

_STATUS_ORDER = ("cloné", "local seul", "orphelin", "distant seul")


def _flags(entry: CatalogEntry, catalog: Catalog) -> str:
    """Drapeaux compacts : ce qui empêche ou motive une action."""
    marks: list[str] = []
    local = entry.local
    if entry.archived:
        marks.append("ARCH")
    if local is not None:
        if local.ahead:
            marks.append(f"{UP}{local.ahead}")
        if local.dirty:
            marks.append(f"M{local.dirty}")
        if local.ref is None:
            marks.append("!SANS-REMOTE")
    if catalog.parents_of(entry.key):
        marks.append("SUB")
    return " ".join(marks)


def render_summary(catalog: Catalog, out=sys.stdout) -> None:
    entries = list(catalog.entries.values())
    local = [e for e in entries if e.local is not None]
    remote = [e for e in entries if e.remote is not None]
    on_disk = sum(e.local.size_bytes for e in local)
    git_part = sum(e.local.git_dir_bytes for e in local)

    print(file=out)
    print(
        f"  {count(len(entries))} dépôts au catalogue   "
        f"{count(len(local))} clonés localement   "
        f"{count(len(remote))} vus sur les forges",
        file=out,
    )
    print(
        f"  {human(on_disk)} sur le disque, dont {human(git_part)} d'historique .git",
        file=out,
    )

    if not catalog.queried_hosts:
        print(
            "  aucune forge interrogée : renseignez GITHUB_TOKEN / GITLAB_TOKEN "
            "dans .env, puis « repos check »",
            file=out,
        )


def render_table(catalog: Catalog, out=sys.stdout, sort: str = "size", limit: int = 0) -> None:
    entries = catalog.sorted_entries(sort)
    if limit:
        entries = entries[:limit]
    if not entries:
        print("\n  Aucun dépôt.", file=out)
        return

    print(file=out)
    print(
        f"  {'état':<12}{'taille':>11}{'.git':>10}  {'activité':<15}{'drapeaux':<18}dépôt",
        file=out,
    )
    for entry in entries:
        local = entry.local
        size = human(entry.size_bytes) if entry.size_bytes else ""
        git_size = human(local.git_dir_bytes) if local and local.git_dir_bytes else ""
        print(
            f"  {entry.status:<12}{size:>11}{git_size:>10}  "
            f"{since(entry.last_activity):<15}{_flags(entry, catalog):<18}{entry.key}",
            file=out,
        )

    print(file=out)
    print(
        "  ARCH archivé · SUB inclus comme submodule · "
        f"{UP}n commits non poussés · Mn fichiers modifiés",
        file=out,
    )


def render_graph(catalog: Catalog, out=sys.stdout) -> None:
    """Graphe des submodules : qui inclut qui."""
    print(file=out)
    print("  Liens entre dépôts (submodules)", file=out)

    if not catalog.edges:
        print(file=out)
        print("  Aucun .gitmodules trouvé.", file=out)
        print(
            "  Les dépôts inspectés sont donc indépendants : aucun n'en embarque un autre.",
            file=out,
        )
        if not catalog.queried_hosts:
            print(
                "  Note : seuls les clones locaux ont été lus. Un submodule déclaré sur "
                "un dépôt\n  non cloné n'apparaîtra qu'après "
                "« repos graph --refresh --submodules ».",
                file=out,
            )
        return

    for parent_key in catalog.roots():
        _render_branch(catalog, parent_key, out, seen=set())

    unknown = [e for e in catalog.edges if not e.child_known]
    if unknown:
        print(file=out)
        print("  Référencés mais absents du catalogue", file=out)
        for edge in unknown:
            target = edge.child_ref.key if edge.child_ref else edge.url
            print(f"    {edge.parent_key}  {ARROW}  {target}", file=out)


def _render_branch(catalog: Catalog, key: str, out, seen: set[str], prefix: str = "") -> None:
    if not prefix:
        print(file=out)
        print(f"  {key}", file=out)

    if key in seen:  # cycle : un submodule peut boucler sur un ancêtre
        print(f"  {prefix}{ELBOW}{DASH} (cycle)", file=out)
        return
    seen = seen | {key}

    edges = catalog.children_of(key)
    for index, edge in enumerate(edges):
        last = index == len(edges) - 1
        connector = (ELBOW if last else TEE) + DASH + " "
        target = edge.child_ref.key if edge.child_ref else edge.url

        state: list[str] = []
        if edge.checked_out:
            state.append("déployé")
        elif edge.child_known:
            state.append("déclaré, non déployé")
        else:
            state.append("hors catalogue")
        entry = catalog.entries.get(edge.child_key or "")
        if entry is not None and entry.local is not None:
            state.append(human(entry.local.size_bytes))

        print(
            f"  {prefix}{connector}{edge.rel_path or '?'}  {ARROW}  {target}"
            f"   [{', '.join(state)}]",
            file=out,
        )

        if edge.child_key and edge.child_known:
            child_prefix = prefix + ("   " if last else f"{PIPE}  ")
            _render_branch(catalog, edge.child_key, out, seen, child_prefix)


def render_reclaimable(catalog: Catalog, out=sys.stdout, limit: int = 20) -> None:
    """Ce qui peut partir sans rien perdre, et ce qui est retenu et pourquoi."""
    safe = catalog.reclaimable()

    print(file=out)
    print("  Supprimables sans perte", file=out)
    if not safe:
        print("    (aucun — voir les motifs ci-dessous)", file=out)
    else:
        total = sum(e.local.size_bytes for e in safe)
        for entry in safe[:limit]:
            print(f"    {human(entry.local.size_bytes):>11}  {entry.key}", file=out)
        print(f"    {human(total):>11}  au total sur {count(len(safe))} dépôt(s)", file=out)

    blocked = [(e, catalog.blockers(e)) for e in catalog.local_entries()]
    blocked = [(e, reasons) for e, reasons in blocked if reasons]
    if blocked:
        print(file=out)
        print("  Retenus", file=out)
        for entry, reasons in sorted(blocked, key=lambda p: -p[0].size_bytes)[:limit]:
            print(f"    {human(entry.size_bytes):>11}  {entry.name}", file=out)
            for reason in reasons:
                print(f"                 {DASH} {reason}", file=out)
