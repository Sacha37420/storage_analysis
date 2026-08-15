"""Ligne de commande : `python -m storage_analysis <commande>`.

Deux familles de commandes :
  scan / info   analyse de l'occupation disque
  repos         catalogue des dépôts GitHub / GitLab et de leurs clones
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime

from . import __version__, cli_repos
from .core import scan, snapshot
from .env import check_interpreter

# --------------------------------------------------------------- formatage --

from .fmt import BAR_EMPTY, BAR_FULL, DASH, ELBOW, PIPE, TEE, bar, count, duration, human

# ----------------------------------------------------------------- affichage --

def print_summary(tree, out=sys.stdout) -> None:
    meta = tree.meta
    total = int(tree.total_size[0])
    elapsed = float(meta.get("elapsed") or 0.0)
    entries = len(tree)
    rate = entries / elapsed if elapsed > 0 else 0.0

    print(file=out)
    print(f"  {tree.root_path}", file=out)
    print(
        f"  {human(total)}   {count(meta.get('files', 0))} fichiers   "
        f"{count(meta.get('dirs', 0))} dossiers",
        file=out,
    )

    detail = []
    if elapsed:
        detail.append(f"scan en {duration(elapsed)}")
    if rate:
        detail.append(f"{count(int(rate))} entrées/s")
    detail.append(f"{meta.get('workers', 1)} thread(s)")
    rotational = meta.get("rotational")
    if rotational is not None:
        detail.append("disque rotatif" if rotational else "SSD/NVMe")
    detail.append("tailles allouées" if meta.get("size_mode") == "allocated" else "tailles logiques")
    print(f"  {' · '.join(detail)}", file=out)

    errors = int(meta.get("errors", 0))
    if errors:
        print(f"  {count(errors)} élément(s) inaccessible(s) — non mesurés", file=out)
    if meta.get("cancelled"):
        print("  scan interrompu : les totaux sont partiels", file=out)


def print_tree(tree, root: int = 0, max_depth: int = 3, top: int = 10,
               min_share: float = 0.01, out=sys.stdout) -> None:
    total = tree.total_size
    root_total = int(total[root]) or 1

    print(file=out)
    print("  Arborescence  (part du dossier parent)", file=out)
    print(f"  {human(root_total):>11}  {'':18}   100 %  {tree.name(root)}", file=out)

    last_flags: list[bool] = []
    for index, level, is_last in tree.walk_by_size(root, max_depth, top, min_share):
        del last_flags[level:]
        last_flags.append(is_last)

        guides = "".join("   " if last_flags[l] else f"{PIPE}  " for l in range(level))
        connector = (ELBOW if is_last else TEE) + DASH + " "

        parent_total = int(total[int(tree.parent[index])]) or 1
        share = int(total[index]) / parent_total
        suffix = os.sep if tree.is_dir(index) else ""
        link = "  ->" if tree.is_link(index) else ""

        print(
            f"  {human(int(total[index])):>11}  {bar(share)}  {share * 100:4.0f} %  "
            f"{guides}{connector}{tree.name(index)}{suffix}{link}",
            file=out,
        )


def print_largest_files(tree, k: int = 15, out=sys.stdout) -> None:
    files = tree.largest_files(k)
    if files.size == 0:
        return
    root = tree.root_path
    print(file=out)
    print(f"  Plus gros fichiers", file=out)
    for index in files.tolist():
        try:
            relative = os.path.relpath(tree.path(index), root)
        except ValueError:
            relative = tree.path(index)
        print(f"  {human(int(tree.size[index])):>11}  {relative}", file=out)


def print_extensions(tree, k: int = 12, out=sys.stdout) -> None:
    rows = tree.extension_totals()[:k]
    if not rows:
        return
    grand_total = int(tree.total_size[0]) or 1
    print(file=out)
    print("  Répartition par extension  (part du total)", file=out)
    for extension, size, number in rows:
        share = size / grand_total
        print(
            f"  {human(size):>11}  {bar(share)}  {share * 100:4.0f} %  "
            f"{extension}  ({count(number)} fichiers)",
            file=out,
        )


# ----------------------------------------------------------------- commandes --

def _default_output(root: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", root).strip("-").lower() or "racine"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join("snapshots", f"{slug}-{stamp}.npz")


def cmd_scan(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"  Dossier introuvable : {root}", file=sys.stderr)
        return 1

    print(f"\n  Scan de {root}", file=sys.stderr)

    started = time.perf_counter()

    def progress(files: int, dirs: int, size: int) -> None:
        if args.quiet:
            return
        elapsed = time.perf_counter() - started
        rate = (files + dirs) / elapsed if elapsed > 0 else 0
        sys.stderr.write(
            f"\r  {count(files)} fichiers · {count(dirs)} dossiers · "
            f"{human(size)} · {count(int(rate))}/s   "
        )
        sys.stderr.flush()

    try:
        tree = scan(
            root,
            workers=args.workers,
            size_mode=args.size_mode,
            max_depth=args.max_depth,
            on_progress=None if args.quiet else progress,
        )
    except KeyboardInterrupt:
        print("\n  Interrompu.", file=sys.stderr)
        return 130

    if not args.quiet:
        sys.stderr.write("\r" + " " * 78 + "\r")
        sys.stderr.flush()

    print_summary(tree)
    print_tree(tree, max_depth=args.tree_depth, top=args.top, min_share=args.min_share)
    print_largest_files(tree, args.files)
    if args.extensions:
        print_extensions(tree)

    if not args.no_save:
        target = args.out or _default_output(root)
        path = snapshot.save(tree, target)
        size_on_disk = os.path.getsize(path)
        # stdout est bufferisé quand il est redirigé : vider avant d'écrire sur
        # stderr, sinon le message s'intercale au milieu du rapport.
        sys.stdout.flush()
        print(f"\n  Snapshot : {path}  ({human(size_on_disk)})", file=sys.stderr)

    print(file=sys.stdout)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Lance l'application fenetree."""
    try:
        from .ui import create_app, launch
    except ImportError as exc:
        print(f"  Interface indisponible : {exc}", file=sys.stderr)
        print(r"  Relancez .\install.ps1 pour installer dash et plotly.", file=sys.stderr)
        return 1

    if args.snapshot and not os.path.isfile(args.snapshot):
        print(f"  Snapshot introuvable : {args.snapshot}", file=sys.stderr)
        return 1

    print("  Ouverture de l'interface...", file=sys.stderr)
    app = create_app(args.snapshot)
    return launch(app, port=args.port, window=not args.browser)


def cmd_info(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.snapshot):
        print(f"  Snapshot introuvable : {args.snapshot}", file=sys.stderr)
        return 1

    tree = snapshot.load(args.snapshot)
    scanned = tree.meta.get("scanned_at")
    if scanned:
        stamp = datetime.fromtimestamp(scanned).strftime("%d/%m/%Y %H:%M")
        print(f"\n  Snapshot du {stamp}", file=sys.stderr)

    print_summary(tree)
    print_tree(tree, max_depth=args.tree_depth, top=args.top, min_share=args.min_share)
    print_largest_files(tree, args.files)
    if args.extensions:
        print_extensions(tree)
    print(file=sys.stdout)
    return 0


# ---------------------------------------------------------------- arguments --

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storage_analysis",
        description="Analyse et visualisation de l'occupation disque.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--allow-any-python",
        action="store_true",
        help="ne pas avertir si l'interpréteur n'est pas celui du .venv du projet",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_view_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--top", type=int, default=10,
                         help="branches affichées par niveau (défaut : 10)")
        sub.add_argument("--tree-depth", type=int, default=2,
                         help="profondeur de l'arborescence affichée (défaut : 2)")
        sub.add_argument("--min-share", type=float, default=0.01,
                         help="part minimale du parent pour être affiché (défaut : 0.01)")
        sub.add_argument("--files", type=int, default=15,
                         help="nombre de fichiers listés (défaut : 15)")
        sub.add_argument("--extensions", action="store_true",
                         help="ajouter la répartition par extension")

    scan_parser = subparsers.add_parser("scan", help="scanner un dossier ou un disque")
    scan_parser.add_argument("path", help="dossier ou lettre de lecteur à analyser")
    scan_parser.add_argument("-o", "--out", help="chemin du snapshot (défaut : snapshots/...)")
    scan_parser.add_argument("--no-save", action="store_true", help="ne pas écrire de snapshot")
    scan_parser.add_argument("-w", "--workers", type=int, default=None,
                             help="threads de scan (défaut : auto, 1 sur disque rotatif)")
    scan_parser.add_argument("--size-mode", choices=("logical", "allocated"), default="logical",
                             help="taille déclarée ou arrondie au cluster (défaut : logical)")
    scan_parser.add_argument("--max-depth", type=int, default=None,
                             help="profondeur maximale de descente")
    scan_parser.add_argument("-q", "--quiet", action="store_true", help="pas de progression")
    add_view_options(scan_parser)
    scan_parser.set_defaults(func=cmd_scan)

    info_parser = subparsers.add_parser("info", help="relire un snapshot existant")
    info_parser.add_argument("snapshot", help="fichier .npz produit par « scan »")
    add_view_options(info_parser)
    info_parser.set_defaults(func=cmd_info)

    ui_parser = subparsers.add_parser("ui", help="ouvrir l'application fenetree")
    ui_parser.add_argument("snapshot", nargs="?",
                           help="snapshot .npz a precharger (facultatif)")
    ui_parser.add_argument("--port", type=int, default=8767,
                           help="port local du serveur de rendu (defaut : 8767)")
    ui_parser.add_argument("--browser", action="store_true",
                           help="ouvrir dans le navigateur au lieu d'une fenetre native")
    ui_parser.set_defaults(func=cmd_ui)

    cli_repos.register(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Sans argument, on ouvre l'application. C'est ce qu'attend un double-clic
    # sur run.bat, et le geste par défaut de l'outil : les sous-commandes texte
    # restent accessibles en les nommant.
    #
    # Une option seule (« run.ps1 --browser ») suit la même logique : puisque la
    # commande implicite est « ui », ses options doivent l'être aussi. Les
    # options du niveau supérieur restent traitées comme telles.
    _TOP_LEVEL = {"-h", "--help", "--version", "--allow-any-python"}
    if not argv:
        argv = ["ui"]
    elif argv[0].startswith("-") and argv[0] not in _TOP_LEVEL:
        argv = ["ui", *argv]

    args = build_parser().parse_args(argv)
    if not args.allow_any_python:
        check_interpreter(strict=False)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
