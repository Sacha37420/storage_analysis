"""Serveur de boucle locale : capte les retours du navigateur.

Le montage oauth-hub fait revenir le navigateur **deux fois** sur le poste, sur
deux chemins d'un même port :

    /callback         retour de Keycloak      (code d'autorisation + state)
    /oauth-hub-done   retour d'oauth-hub      (métadonnées de connexion)

D'où un serveur qui reste lié au port entre les deux attentes, plutôt qu'un
serveur par retour : rouvrir le port entre les deux exposerait à le trouver pris
au second tour, et l'URI est enregistrée dans Keycloak, donc non négociable.

On écoute sur 127.0.0.1 et jamais 0.0.0.0 : le port ne doit pas être joignable
depuis le réseau, sinon un voisin pourrait intercepter le code d'autorisation.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from typing import Any

_PAGE = """<!doctype html><meta charset="utf-8">
<title>storage_analysis</title>
<body style="font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center;max-width:34rem;padding:2rem">
<h1 style="font-size:1.3rem;margin:0 0 .5rem">{title}</h1>
<p style="color:#666;margin:0">{message}</p>
</div></body>"""


class LoopbackError(RuntimeError):
    """Le port est indisponible, ou aucun retour n'est arrivé à temps."""


class _Handler(http.server.BaseHTTPRequestHandler):
    server: "LoopbackServer"  # renseigné à l'instanciation du serveur

    def do_GET(self) -> None:  # noqa: N802 - imposé par BaseHTTPRequestHandler
        state = self.server.state  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path != state.get("wanted"):
            self.send_error(404)
            return

        state["received"] = dict(urllib.parse.parse_qsl(parsed.query))
        state["event"].set()

        failed = "oauth_error" in state["received"] or "error" in state["received"]
        body = _PAGE.format(
            title="Échec" if failed else "C'est bon.",
            message=(
                state["received"].get("oauth_error", "L'autorisation a échoué.")
                if failed else
                "Vous pouvez fermer cet onglet et revenir à l'application."
            ),
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Rien à mettre en cache, et surtout rien à laisser derrière soi.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence : le serveur ne doit rien écrire dans la sortie de la commande."""


class LoopbackServer:
    """Serveur éphémère, à utiliser comme gestionnaire de contexte.

        with LoopbackServer(8765) as loopback:
            ...
            reponse = loopback.wait_for("/callback")
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.state: dict[str, Any] = {"wanted": None, "received": {}, "event": threading.Event()}
        try:
            self._server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            raise LoopbackError(
                f"Impossible d'écouter sur 127.0.0.1:{port} ({exc}). "
                f"Le port est enregistré dans Keycloak : libérez-le, ou changez-le "
                f"des deux côtés."
            ) from exc
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    # --------------------------------------------------------------- cycle --

    def __enter__(self) -> "LoopbackServer":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._server.shutdown()
        self._server.server_close()
        return False

    # -------------------------------------------------------------- attente --

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def wait_for(self, path: str, timeout: float = 300.0) -> dict[str, str]:
        """Attend un retour du navigateur sur `path` et rend ses paramètres."""
        self.state["wanted"] = path
        self.state["received"] = {}
        self.state["event"] = threading.Event()

        if not self.state["event"].wait(timeout=timeout):
            raise LoopbackError(
                f"Aucun retour du navigateur sur {path} après {int(timeout)} s."
            )
        return dict(self.state["received"])
