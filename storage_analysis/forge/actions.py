"""Actions sur les dépôts, et les garde-fous qui vont avec.

Trois gestes de portées très différentes :

  * `delete_local`  — récupère de la place, réversible tant que la corbeille
                      n'est pas vidée ;
  * `set_archived`  — marque un projet distant en lecture seule, réversible ;
  * `delete_remote` — détruit le projet sur la forge, irréversible.

Le troisième est le seul qui puisse faire perdre le travail d'autrui : sur une
instance partagée, un projet de groupe n'appartient pas qu'à celui qui le
supprime. D'où trois barrières cumulatives — confirmation par saisie du chemin
complet, liste de namespaces protégés, refus si aucun clone local ne subsiste —
et un journal d'audit de tout ce qui est tenté.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from dataclasses import dataclass

from ..env import PROJECT_ROOT
from .catalog import Catalog
from .clients import ForgeClient, ForgeError
from .models import CatalogEntry

AUDIT_LOG = PROJECT_ROOT / "logs" / "forge-actions.log"


@dataclass(slots=True)
class ActionOutcome:
    ok: bool
    message: str
    freed_bytes: int = 0
    blocked_by: list[str] | None = None


def _audit(action: str, target: str, outcome: ActionOutcome, dry_run: bool) -> None:
    """Journalise toute tentative, réussie ou non. Best effort."""
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "target": target,
        "dry_run": dry_run,
        "ok": outcome.ok,
        "message": outcome.message,
        "freed_bytes": outcome.freed_bytes,
    }
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def is_protected(entry: CatalogEntry, patterns: list[str]) -> str | None:
    """Renvoie le motif qui protège cette entrée, ou None."""
    if entry.ref is None:
        return None
    candidates = (entry.ref.key, entry.ref.path, entry.ref.namespace)
    for pattern in patterns:
        if any(fnmatch.fnmatch(value, pattern) for value in candidates):
            return pattern
    return None


# ------------------------------------------------------------------ local --

def delete_local(
    entry: CatalogEntry,
    catalog: Catalog,
    *,
    force: bool = False,
    dry_run: bool = False,
    to_trash: bool = True,
) -> ActionOutcome:
    """Supprime le clone local, vers la corbeille par défaut."""
    if entry.local is None:
        outcome = ActionOutcome(False, "aucun clone local à supprimer")
        _audit("delete_local", entry.key, outcome, dry_run)
        return outcome

    path = entry.local.path
    size = entry.local.size_bytes
    blockers = catalog.blockers(entry)

    if blockers and not force:
        outcome = ActionOutcome(
            False,
            "suppression refusée — " + " ; ".join(blockers),
            blocked_by=blockers,
        )
        _audit("delete_local", path, outcome, dry_run)
        return outcome

    if dry_run:
        note = " (garde-fous ignorés par --force)" if blockers else ""
        outcome = ActionOutcome(True, f"supprimerait {path}{note}", freed_bytes=size)
        _audit("delete_local", path, outcome, dry_run)
        return outcome

    try:
        if to_trash:
            from send2trash import send2trash
            send2trash(os.fspath(path))
            where = "corbeille"
        else:
            shutil.rmtree(path)
            where = "définitivement supprimé"
    except Exception as exc:  # send2trash lève des exceptions maison
        outcome = ActionOutcome(False, f"échec de la suppression : {exc}")
        _audit("delete_local", path, outcome, dry_run)
        return outcome

    outcome = ActionOutcome(True, f"{path} -> {where}", freed_bytes=size)
    _audit("delete_local", path, outcome, dry_run)
    return outcome


# --------------------------------------------------------------- archivage --

def set_archived(
    entry: CatalogEntry,
    client: ForgeClient,
    archived: bool,
    *,
    dry_run: bool = False,
) -> ActionOutcome:
    """Archive ou désarchive le projet distant. Réversible."""
    verb = "archiverait" if archived else "désarchiverait"
    action = "archive" if archived else "unarchive"

    if entry.remote is None:
        outcome = ActionOutcome(False, "aucun projet distant connu pour cette entrée")
        _audit(action, entry.key, outcome, dry_run)
        return outcome

    if entry.remote.archived == archived:
        state = "déjà archivé" if archived else "déjà actif"
        outcome = ActionOutcome(True, state)
        _audit(action, entry.key, outcome, dry_run)
        return outcome

    if dry_run:
        outcome = ActionOutcome(True, f"{verb} {entry.key}")
        _audit(action, entry.key, outcome, dry_run)
        return outcome

    try:
        client.set_archived(entry.remote, archived)
    except ForgeError as exc:
        outcome = ActionOutcome(False, str(exc))
        _audit(action, entry.key, outcome, dry_run)
        return outcome

    entry.remote.archived = archived
    outcome = ActionOutcome(True, f"{entry.key} {'archivé' if archived else 'désarchivé'}")
    _audit(action, entry.key, outcome, dry_run)
    return outcome


# ------------------------------------------------------ suppression distante --

def delete_remote(
    entry: CatalogEntry,
    client: ForgeClient,
    *,
    confirmation: str | None,
    protected: list[str],
    dry_run: bool = False,
    allow_last_copy: bool = False,
) -> ActionOutcome:
    """Supprime le projet sur la forge. Irréversible.

    Trois barrières avant l'appel : chemin complet saisi à l'identique,
    namespace non protégé, et existence d'un clone local — sauf dérogation.
    """
    target = entry.key

    if entry.remote is None:
        outcome = ActionOutcome(False, "aucun projet distant connu pour cette entrée")
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    expected = entry.ref.path if entry.ref else entry.key
    if confirmation != expected:
        outcome = ActionOutcome(
            False,
            f"confirmation absente ou incorrecte : attendu exactement « {expected} »",
        )
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    pattern = is_protected(entry, protected)
    if pattern:
        outcome = ActionOutcome(
            False,
            f"namespace protégé par FORGE_PROTECTED (« {pattern} ») — "
            "retirez le motif du .env pour autoriser.",
        )
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    if entry.local is None and not allow_last_copy:
        outcome = ActionOutcome(
            False,
            "aucun clone local : la suppression distante détruirait la dernière copie "
            "connue. Utilisez --allow-last-copy si c'est bien l'intention.",
        )
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    if dry_run:
        outcome = ActionOutcome(True, f"supprimerait le projet distant {target}")
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    try:
        client.delete(entry.remote)
    except ForgeError as exc:
        outcome = ActionOutcome(False, str(exc))
        _audit("delete_remote", target, outcome, dry_run)
        return outcome

    outcome = ActionOutcome(
        True,
        f"{target} supprimé sur {entry.remote.forge} "
        "(GitLab peut appliquer une suppression différée selon l'instance)",
    )
    _audit("delete_remote", target, outcome, dry_run)
    return outcome
