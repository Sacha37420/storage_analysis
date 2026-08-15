"""Hôte de l'application : une vraie fenêtre, avec repli navigateur.

Le rendu est fait par Dash, qui parle HTTP. Pour que cela reste une
**application** et non un onglet, la page est affichée dans une fenêtre native
(pywebview, qui s'appuie sur WebView2 sous Windows). Si ce composant manque,
on ouvre le navigateur plutôt que d'échouer : l'outil doit rester utilisable.

Le serveur n'écoute que sur 127.0.0.1 — il expose le contenu du disque, il n'a
rien à faire sur le réseau.
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser

DEFAULT_PORT = 8767


def _free_port(preferred: int) -> int:
    """Le port demandé s'il est libre, sinon un port attribué par le système.

    La sonde active SO_REUSEADDR comme le fait Werkzeug : sans cela, un socket
    encore en TIME_WAIT ferait conclure à tort que le port est pris, et
    l'application basculerait sur un port aléatoire après chaque fermeture.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_up(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def launch(
    app,
    *,
    port: int = DEFAULT_PORT,
    window: bool = True,
    width: int = 1440,
    height: int = 900,
) -> int:
    """Démarre le serveur puis ouvre la fenêtre. Rend un code de sortie."""
    port = _free_port(port)
    url = f"http://127.0.0.1:{port}/"

    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server.start()

    if not _wait_until_up(port):
        print(f"  Le serveur local n'a pas démarré sur {url}")
        return 1

    if window:
        try:
            import webview
        except ImportError:
            print("  pywebview absent : ouverture dans le navigateur.")
            print(f"  (« .\\install.ps1 » l'installe ; l'application reste utilisable ainsi)")
        else:
            webview.create_window("Analyseur d'espace disque", url,
                                  width=width, height=height, min_size=(1024, 640))
            # Bloque jusqu'à la fermeture de la fenêtre ; le serveur meurt avec
            # le processus puisque son fil est daemon.
            webview.start()
            return 0

    print(f"  Interface disponible sur {url}")
    print("  Ctrl+C pour quitter.")
    webbrowser.open(url)
    try:
        while server.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    return 0
