"""Vue « dépôts » : corrélation entre clones locaux et forges GitHub / GitLab.

Le lien avec l'analyse d'espace disque est direct : un clone local occupe de la
place, et la décision de le supprimer dépend de ce qui existe *ailleurs* —
est-il poussé, archivé, référencé comme submodule par un autre dépôt ?

Découpage :
    urls.py      normalisation des URL de remote en (hôte, chemin)
    models.py    structures de données, sans logique
    local.py     découverte des clones et lecture de leur état git
    clients.py   accès API GitHub et GitLab
    catalog.py   union local/distant et graphe de submodules
    actions.py   suppression locale, archivage et suppression distante
"""

from .catalog import Catalog, build_catalog
from .models import CatalogEntry, LocalRepo, RemoteRepo, RepoRef, SubmoduleDecl

__all__ = [
    "Catalog",
    "build_catalog",
    "CatalogEntry",
    "LocalRepo",
    "RemoteRepo",
    "RepoRef",
    "SubmoduleDecl",
]
