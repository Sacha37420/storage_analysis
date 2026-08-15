"""Interface graphique : Dash pour le rendu, fenêtre native pour l'hôte."""

from .app import create_app
from .window import launch

__all__ = ["create_app", "launch"]
