r"""Vérifications du lot 0 sur une arborescence fabriquée.

Volontairement sans pytest : le projet ne dépend que de son requirements.txt.

    .\.venv\Scripts\python.exe tests\verify_lot0.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_analysis.core import scan, snapshot
from storage_analysis.core.tree import FLAG_DIR, FLAG_LINK

failures = []


def check(label, got, expected):
    ok = got == expected
    print(f"  {'OK ' if ok else 'ECHEC'}  {label}: {got!r}" + ("" if ok else f"  (attendu {expected!r})"))
    if not ok:
        failures.append(label)


root = Path(tempfile.mkdtemp(prefix="sa-verify-"))
(root / "a").mkdir()
(root / "a" / "b").mkdir()
(root / "vide").mkdir()
(root / "a" / "f1.bin").write_bytes(b"x" * 1000)
(root / "a" / "b" / "f2.bin").write_bytes(b"x" * 2000)
(root / "a" / "b" / "f3.txt").write_bytes(b"x" * 3)
(root / "top.bin").write_bytes(b"x" * 500)

# Jonction vers un dossier déjà compté : ne doit être ni suivie ni recomptée.
junction_ok = False
if os.name == "nt":
    rc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "lien"), str(root / "a")],
        capture_output=True, text=True,
    )
    junction_ok = rc.returncode == 0

print(f"\narborescence de test : {root}  (jonction créée : {junction_ok})")

print("\n--- totaux ---")
tree = scan(root, workers=1)
check("taille totale", int(tree.total_size[0]), 3503)
check("nombre de fichiers", int(tree.file_count[0]), 4)
check("dossiers (racine incluse)", int(tree.dir_count[0]), 4 + (1 if junction_ok else 0))
check("entrées totales", len(tree), 4 + 4 + (1 if junction_ok else 0))

if junction_ok:
    links = [i for i in range(len(tree)) if tree.flags[i] & FLAG_LINK]
    check("jonction repérée", len(links), 1)
    check("jonction non parcourue", int(tree.total_size[links[0]]), 0)

print("\n--- multi-thread identique au mono-thread ---")
tree8 = scan(root, workers=8)
check("total 8 threads", int(tree8.total_size[0]), 3503)
check("entrées 8 threads", len(tree8), len(tree))

print("\n--- taille allouée (arrondi cluster) ---")
alloc = scan(root, workers=1, size_mode="allocated")
check("allouée >= logique", bool(int(alloc.total_size[0]) >= 3503), True)
check("cluster relevé", isinstance(alloc.meta["cluster_size"], int), True)

print("\n--- chemins et navigation ---")
by_name = {tree.name(i): i for i in range(len(tree))}
check("chemin de f2.bin", tree.path(by_name["f2.bin"]), str(root / "a" / "b" / "f2.bin"))
check("cumul de a", int(tree.total_size[by_name["a"]]), 3003)
check("dossier vide à 0", int(tree.total_size[by_name["vide"]]), 0)
check("enfants de la racine triés", tree.name(int(tree.children_by_size(0, 1)[0])), "a")

print("\n--- cas limites ---")
check("largest_files(0)", tree.largest_files(0).size, 0)
check("largest_files(2)", [tree.name(i) for i in tree.largest_files(2)], ["f2.bin", "f1.bin"])
check("children_by_size(limit=0)", tree.children_by_size(0, 0).size, 0)
check("walk top=0", len(list(tree.walk_by_size(0, 3, 0, 0.0))), 0)
check("subtree(a)", len(tree.subtree(by_name["a"])), 5)  # a, a/b, f1, f2, f3
check("max_depth=1", int(scan(root, workers=1, max_depth=1).total_size[0]), 500)

print("\n--- snapshot aller-retour ---")
target = root / "snap.npz"
snapshot.save(tree, target)
back = snapshot.load(target)
check("total conservé", int(back.total_size[0]), int(tree.total_size[0]))
check("noms conservés", back.name(by_name["f3.txt"]), "f3.txt")
check("racine conservée", back.root_path, tree.root_path)
check("meta conservée", back.meta["files"], tree.meta["files"])

print("\n--- extensions ---")
extensions = dict((e, s) for e, s, _ in tree.extension_totals())
check("total .bin", extensions[".bin"], 3500)
check("total .txt", extensions[".txt"], 3)

shutil.rmtree(root, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} vérification(s) en échec : {failures}")
    sys.exit(1)
print("Toutes les vérifications passent.")
