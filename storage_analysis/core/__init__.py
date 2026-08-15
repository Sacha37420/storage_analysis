"""Coeur de l'analyseur : scan, modèle d'arbre, persistance.

Ce paquet ne dépend d'aucune interface graphique : il expose un modèle
interrogeable que l'UI (Dash, Qt, TUI...) consomme sans le contraindre.
"""

from .scanner import scan
from .snapshot import load, save
from .tree import FLAG_DIR, FLAG_ERROR, FLAG_LINK, FileTree

__all__ = ["scan", "save", "load", "FileTree", "FLAG_DIR", "FLAG_LINK", "FLAG_ERROR"]
