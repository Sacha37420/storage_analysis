"""Formatage partagé par les vues texte (analyse disque et catalogue de dépôts)."""

from __future__ import annotations

import sys
import time

_UNITS = ("o", "Kio", "Mio", "Gio", "Tio", "Pio")


def _can_print(sample: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# Les consoles Windows héritées ne savent pas encoder les demi-blocs : on
# bascule sur de l'ASCII plutôt que de planter à l'affichage.
RICH = _can_print("█░│├└─→")
BAR_FULL, BAR_EMPTY = ("█", "░") if RICH else ("#", ".")
PIPE, TEE, ELBOW, DASH = ("│", "├", "└", "─") if RICH else ("|", "+", "+", "-")
ARROW = "→" if RICH else "->"
UP = "↑" if RICH else "^"


def human(n: int | float) -> str:
    """Taille lisible, en unités binaires (1 Kio = 1024 o)."""
    value = float(n)
    unit = 0
    while abs(value) >= 1024.0 and unit < len(_UNITS) - 1:
        value /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(n)} o"
    return f"{value:,.1f} {_UNITS[unit]}".replace(",", " ").replace(".", ",")


def count(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def bar(share: float, width: int = 18) -> str:
    filled = max(0, min(width, int(round(share * width))))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} min {rest:.0f} s"


def since(epoch: int | None) -> str:
    """Ancienneté lisible : « il y a 3 mois », « aujourd'hui », « — »."""
    if not epoch:
        return "—" if RICH else "-"
    delta = time.time() - epoch
    if delta < 0:
        return "à l'instant"
    days = delta / 86400
    if days < 1:
        return "aujourd'hui"
    if days < 2:
        return "hier"
    if days < 31:
        return f"il y a {int(days)} j"
    if days < 365:
        return f"il y a {int(days / 30.44)} mois"
    years = days / 365.25
    return f"il y a {years:.0f} an" + ("s" if years >= 2 else "")
