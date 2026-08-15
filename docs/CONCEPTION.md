# Analyseur d'espace disque — état de l'art & conception

> Document de conception initial. Cible : Windows 10/11 (NTFS) en priorité, portable Linux/macOS.
> Python 3.13.

---

## 1. État de l'art des représentations

### 1.1 Treemap (pavage rectangulaire)

Inventé par Shneiderman (1991) précisément pour le problème « qui remplit mon disque ». Surface ∝ taille,
imbrication = hiérarchie. C'est la famille dominante (WinDirStat, SpaceSniffer, TreeSize, WizTree, Baobab).

| Variante | Principe | Force | Faiblesse |
|---|---|---|---|
| **Slice-and-dice** | découpe alternée H/V | ordre préservé, layout stable | rectangles filiformes, surfaces incomparables |
| **Squarified** (Bruls & al. 2000) | vise un ratio d'aspect ≈ 1 | **lisibilité et comparaison des surfaces** | perd l'ordre, instable (petit delta de données → layout très différent) |
| **Ordered / strip / pivot** (Bederson & Shneiderman 2002) | compromis | ordre + ratio corrects, transitions fluides | ratio un peu moins bon que squarified |
| **Cushion** (van Wijk) | ombrage 3D par niveau | la hiérarchie redevient lisible sans bordures | purement cosmétique, coût de rendu |
| **Voronoi** | polygones convexes | esthétique, formes libres | estimation des aires très mauvaise, coût de calcul élevé |
| **Circle packing** | cercles imbriqués | hiérarchie très lisible | ~30 % de surface perdue → proportions faussées |

**Constat retenu** : *squarified + cushion* est le meilleur point de fonctionnement pour la tâche « repérer la
masse ». La faiblesse de stabilité est sans importance ici : on n'anime pas des données qui changent en continu.

### 1.2 Sunburst / anneaux radiaux

Angle ∝ taille, rayon = profondeur (Filelight, DaisyDisk, Baobab).

- **+** : la structure hiérarchique est immédiatement lisible ; la navigation par « creusement » est naturelle.
- **−** : à angle égal, un secteur externe couvre **plus de surface** qu'un secteur interne → biais perceptif
  systématique ; les libellés sont courbés donc peu lisibles ; illisible au-delà de ~5 niveaux.
- Astuce DaisyDisk à reprendre : tri des secteurs par taille + regroupement des petits en « autres éléments ».

### 1.3 Icicle / partition rectangulaire (flame graph)

Équivalent cartésien du sunburst : largeur ∝ taille, une ligne par niveau de profondeur.

- **+** : pas de biais de surface, **libellés horizontaux donc lisibles**, comparaison directe entre frères,
  chemin racine→feuille lu verticalement. C'est le format des flamegraphs, très éprouvé.
- **−** : moins compact que le treemap (une ligne par niveau) ; peu répandu dans les outils grand public.

### 1.4 Arbre / tableau trié avec barres proportionnelles

ncdu, gdu, dust, TreeSize, WizTree. Visuellement pauvre, **opérationnellement le plus efficace** :
chiffres exacts, tri, chemin complet, sélection, action. C'est la vue depuis laquelle on supprime réellement.

### 1.5 Vues transversales (non hiérarchiques)

Souvent négligées, et pourtant celles qui répondent le plus vite à « que puis-je supprimer ? » :
répartition par **type/extension**, par **âge** (dernier accès / modification → données froides),
**top N fichiers**, **doublons** (hash), **histogramme des tailles** (coût réel des millions de petits fichiers),
**diff de deux snapshots** (« qu'est-ce qui a grossi cette semaine ? »).

---

## 2. Représentations retenues

Principe directeur : **une vue pour repérer, une vue pour agir, liées par la sélection.**
Aucune représentation unique ne fait bien les deux.

| # | Vue | Rôle | Techno |
|---|---|---|---|
| **V1** | **Treemap squarifiée** (ombrage cushion, profondeur limitée à 3–4 niveaux, zoom au clic + fil d'Ariane) | repérage instantané de la masse | Plotly `go.Treemap` |
| **V2** | **Table/arbre trié** (taille, % du parent, nb de fichiers, date, barre en ligne) | lecture exacte, tri, action | Dash `DataTable` |
| **V3** | **Icicle** | comprendre la structure et la profondeur du chemin | Plotly `go.Icicle` |
| **V4** | **Top 100 fichiers** du sous-arbre courant | gain immédiat par suppression unitaire | table |
| **V5** | **Répartition par extension / catégorie** (barres horizontales triées) | « 40 % du disque = vidéos » | barres |
| **V6** | **Carte d'âge** (taille × dernier accès) | isoler les données froides | histogramme empilé |
| V7 | *Sunburst* — optionnel | esthétique, capture d'écran | Plotly `go.Sunburst` |
| V8 | *Diff de snapshots* — phase 3 | évolution dans le temps | treemap divergente ± |

V1, V2 et V3 partagent le **même nœud courant** : cliquer dans le treemap filtre la table, et inversement.

**Règle de rendu non négociable — l'élagage adaptatif.** Ne jamais envoyer plus de ~2 000 secteurs au moteur
de rendu : sous chaque nœud, on garde les enfants jusqu'à 95 % de la masse cumulée (ou les 40 plus gros) et on
agrège le reste en un nœud synthétique « … 1 243 autres éléments — 2,1 Go ». Sans cela, toute UI s'effondre sur
un vrai disque et l'écran devient une poussière de pixels illisible.

---

## 3. Méthode de scan

### 3.1 Backend par défaut — `os.scandir()`

- Parcours **itératif** avec pile explicite (jamais récursif : chemins profonds → `RecursionError`).
- `os.scandir()` évite un `stat()` par entrée : l'API Windows `FindFirstFile`/`FindNextFile` renvoie déjà
  taille, attributs et dates. Gain de ~7–50× sur Windows par rapport à un `os.walk` naïf (PEP 471).
- **Ne jamais suivre** liens symboliques, jonctions et points de montage (`entry.is_junction()`,
  `entry.is_symlink()`) : boucles infinies et double comptage garantis sinon.
- Préfixe `\\?\` sur Windows pour franchir la limite `MAX_PATH` (260 caractères).
- Chaque `OSError` / `PermissionError` est capturée **par nœud**, comptabilisée, puis affichée en fin de scan
  (« 312 dossiers inaccessibles, 4,2 Go non mesurés ») — jamais fatale.
- Ordre de grandeur attendu : 50 000–200 000 entrées/s sur SSD NTFS, soit ~10–30 s pour 1 M de fichiers.

### 3.2 Parallélisme

Le scan est I/O-bound et le GIL est relâché pendant les appels système → un `ThreadPoolExecutor` alimenté par
une file de répertoires donne 2–4× sur SSD/NVMe. **Mais c'est contre-productif sur HDD** : les seeks concurrents
détruisent le débit. D'où un paramètre `--workers`, défaut 8 sur SSD et **1 sur disque rotatif** (détection via
`DeviceIoControl / StorageDeviceSeekPenaltyProperty` sur Windows, `/sys/block/*/queue/rotational` sur Linux).

### 3.3 Voie rapide NTFS (phase 2, optionnelle)

Lecture directe de la **MFT** via `DeviceIoControl(FSCTL_ENUM_USN_DATA)` : on récupère tous les enregistrements
`USN_RECORD_V2` (nom, `FileReferenceNumber`, `ParentFileReferenceNumber`, attributs) et on **reconstruit
l'arbre en mémoire** par jointure parent/enfant. C'est le mécanisme de WizTree : jusqu'à ~46× plus rapide,
un volume de 1 To scanné en quelques secondes.

Contraintes : NTFS uniquement, **droits administrateur**, et l'enregistrement USN ne contient pas la taille —
il faut la compléter (`GetFileInformationByHandleEx`, ou lecture de l'attribut `$DATA` de la MFT).
→ backend enfichable, avec **repli automatique et silencieux** sur `scandir` s'il est indisponible.

### 3.4 Que mesure-t-on exactement ?

Trois notions distinctes, à ne pas confondre — c'est la source n°1 d'écarts avec l'Explorateur Windows :

1. **Taille logique** (`st_size`) — défaut, gratuite.
2. **Taille allouée** — arrondi à la taille de cluster (`GetDiskFreeSpace`), quasi gratuite, et corrige le biais
   massif des arborescences à millions de petits fichiers (`node_modules`, dépôts git).
3. **Taille réellement occupée** — `GetCompressedFileSize`, seule exacte pour les fichiers compressés ou sparse
   (VHD, bases de données), mais coûteuse (un appel par fichier) → mode « précis » explicite.

**Hardlinks / doublons d'inode** : ne compter `(st_dev, st_ino)` qu'une fois (comportement de `du` Unix), sinon
surcomptage. Sur Windows, obtenir `st_ino` impose d'ouvrir le fichier → réservé au mode précis.

### 3.5 Modèle de données en mémoire

Piège classique : un objet Python par fichier ≈ 500 octets l'unité, soit **plusieurs Go pour 1 M de fichiers**.

Solution : **tableaux parallèles** (`array` / NumPy), un index entier par entrée :

```
parent[i]   : int32     index du dossier parent
size[i]     : int64     taille propre
name_off[i] : int32     offset dans un unique blob UTF-8 de noms
flags[i]    : uint8     dossier / fichier / lien / erreur
mtime[i]    : int32     epoch
```

≈ **35–40 octets par entrée**, soit ~40 Mo pour 1 M de fichiers, avec une agrégation post-ordre vectorisée
(tri par profondeur décroissante puis `np.add.at(total, parent, total)`) en quelques dizaines de millisecondes.

### 3.6 Snapshots

Sérialisation du scan pour : rouvrir sans re-scanner, comparer deux dates (V8) et déboguer sur données réelles.

**Format retenu : archive `.npz`** (tableaux NumPy bruts + métadonnées JSON), et non SQLite ou Parquet comme
envisagé initialement : le modèle *est* déjà une pile de tableaux alignés, donc l'écriture et la relecture sont
directes, sans dépendance ni conversion. Mesuré : 2,5 Mo pour 179 000 entrées, soit ~15 Mo pour 1 M.
SQLite redeviendra pertinent le jour où il faudra des requêtes partielles sans charger tout l'arbre.

---

## 4. Architecture applicative

```
storage_analysis/
├── core/
│   ├── scanner.py      # backends scandir | mft, progression, annulation
│   ├── tree.py         # tableaux parallèles, agrégation, requêtes de sous-arbre
│   ├── prune.py        # élagage adaptatif pour le rendu
│   ├── snapshot.py     # persistance + diff
│   └── analyze.py      # extensions, âge, top-N, doublons
├── ui/                 # Dash : layout, callbacks, vues V1..V6
├── cli.py              # scan / report / diff
└── docs/
```

Le **cœur ne connaît aucune UI** : il expose un modèle interrogeable. L'UI est remplaçable (Dash aujourd'hui,
PySide6/Qt ou TUI Textual demain) sans toucher au scanner.

**Stack v1** : stdlib + NumPy pour le cœur, Dash/Plotly pour l'UI locale (treemap / icicle / sunburst natifs,
avec zoom et fil d'Ariane), `send2trash` pour les actions.
Alternative envisagée puis écartée pour la v1 : PySide6 + `QGraphicsView` — rendu supérieur au-delà de
50 000 rectangles, mais il faut écrire le layout treemap à la main.

**Sécurité des actions destructives** : suppression **vers la corbeille** par défaut, jamais `os.remove`,
confirmation explicite affichant l'espace récupéré, refus sur les chemins système.

---

## 5. Lots de livraison

| Lot | Contenu | Sortie |
|---|---|---|
| **0** ✅ | scanner `scandir` + modèle compact + CLI `scan` | snapshot + résumé texte |
| **1** ✅ | V1 treemap + V2 table liées, drill-down, nettoyage | app fenêtrée |
| **2** ✅ | V3 icicle, V5 extensions, couleur par ancienneté | app complète |
| **3** | export HTML autonome (fichier unique partageable) | rapport |
| **4** | backend MFT, doublons par hash, V8 diff, suppression corbeille | outil complet |
| **5** ✅ | vue dépôts : catalogue GitHub/GitLab, graphe de submodules, actions | commande `repos` + section de l'application |

---

## 6. Vue dépôts — GitHub / GitLab (lot 5)

### 6.1 Pourquoi c'est le même problème

Un clone occupe de la place, mais sa **suppressibilité** ne se lit pas sur le disque : elle dépend de ce qui
existe ailleurs. Un dépôt de 20 Gio parfaitement poussé est un candidat évident ; le même dépôt avec trois
commits locaux est intouchable. La vue dépôts est donc la couche qui transforme « voici ce qui pèse » en
« voici ce que tu peux enlever ».

### 6.2 Corrélation

Tout repose sur une **clé normalisée** `hôte/namespace/projet`. Les trois écritures d'un même remote
(SCP `git@h:ns/p.git`, `ssh://`, `https://`) convergent vers la même clé, ce qui permet de rapprocher :

* un clone local et son projet distant ;
* l'URL déclarée par un submodule et l'entrée correspondante du catalogue.

Sans cette normalisation, aucun lien n'est calculable.

### 6.3 Sources d'information, délibérément séparées

| Source | Donne | Disponible |
|---|---|---|
| `.git/config`, `.gitmodules` | remotes, submodules déclarés | toujours, sans git installé |
| commande `git` | branche, fichiers modifiés, commits non poussés, date du dernier commit | si git est présent |
| API de la forge | archivage, taille distante, dernière activité, projets non clonés | si un jeton est configuré |

Chaque source dégrade proprement : sans git le catalogue existe encore, sans jeton il reste local. Une
information absente n'est jamais présentée comme une information négative — c'est pourquoi le catalogue
retient les hôtes réellement interrogés, faute de quoi « projet non listé » serait lu comme « projet
supprimé ».

### 6.4 Graphe de submodules

Les submodules forment un DAG entre dépôts. Le catalogue en extrait des arêtes
`parent -> enfant` portant trois qualités : l'enfant est-il **connu** du catalogue, est-il **déployé** sur
le disque, et quelle place occupe-t-il. Trois cas se distinguent alors visuellement : lien résolu et déployé,
lien résolu mais absent du disque, et lien pointant hors catalogue — ce dernier cas signalant un dépôt
tiers ou un projet auquel on n'a pas accès.

Le rendu est un arbre à partir des racines (dépôts qui incluent sans être inclus), avec détection de cycle.

### 6.5 Garde-fous

La suppression distante est irréversible et, sur une instance partagée, elle touche le travail d'autrui.
Trois barrières cumulatives, plus un journal d'audit de toute tentative :

1. **confirmation** par saisie du chemin complet, à l'identique ;
2. **namespaces protégés** (`FORGE_PROTECTED`), motifs glob refusés d'office ;
3. **refus si aucun clone local ne subsiste**, sauf dérogation explicite.

La suppression locale, elle, refuse par défaut dès qu'un travail serait perdu : pas de remote, commits non
poussés, fichiers modifiés, ou dépôt inclus comme submodule par un autre dépôt présent. Elle passe par la
corbeille, jamais par un effacement direct.

### 6.6 Cache

Mesurer la taille de plusieurs Go de clones sur un disque rotatif froid coûte des dizaines de secondes, et
interroger deux forges ajoute des appels réseau. Le catalogue est donc mis en cache en JSON — format
volontairement lisible pour rester inspectable — avec un âge maximal paramétrable, invalidé après toute
action destructive.

---

## 7. Authentification GitHub

L'objectif fixé est celui d'un **client public distribuable** : l'auteur enregistre une application OAuth
une fois, et n'importe qui peut ensuite s'en servir pour son propre compte. C'est un cas parfaitement
standard — le `client_id` est un identifiant public, pas un secret, et `gh`, `az` ou `docker login`
embarquent le leur dans un binaire téléchargeable par tous.

Ce qu'un `client_id` public ne permet jamais : accéder au compte d'autrui. Chaque utilisateur autorise
lui-même sur github.com, avec ses propres identifiants, et reçoit un jeton lié à son seul compte.

### 7.1 Pourquoi PKCE et pas le device flow

Le device flow paraît séduisant pour un outil en ligne de commande — ni secret, ni redirection. C'est
justement l'absence de redirection qui le disqualifie ici. Un attaquant qui connaît le `client_id` public
peut déclencher le flux depuis sa propre machine, envoyer le `user_code` à une victime, et récupérer le
jeton dès que celle-ci a saisi le code. La victime voit sur l'écran de consentement **le nom de notre
application** : notre réputation devient l'appât.

Ce n'est pas théorique — c'est outillé publiquement contre GitHub (GitPhish, travaux de Praetorian), avec
des taux de réussite supérieurs à 90 % en engagement autorisé, et la documentation GitHub l'écrit
elle-même : *« an attacker can use the device flow to remotely impersonate your app as part of a phishing
attack »*, avec la consigne de ne pas l'activer hors environnement contraint.

**Le code d'autorisation avec PKCE ferme cette porte** : l'autorisation ne peut aboutir que sur l'URI de
rappel enregistrée dans l'application, ici `http://127.0.0.1:8765/callback`. Un attaquant distant ne reçoit
donc jamais le code ; et l'aurait-il qu'il lui manquerait le `code_verifier`, tiré aléatoirement à chaque
tentative et jamais transmis sur le réseau — seul son condensé SHA-256 circule.

GitHub ne supportait pas PKCE pour les applications OAuth avant juillet 2025 ; c'est désormais le cas, et
c'est la recommandation officielle pour un client public.

### 7.2 Conséquences pratiques

| Décision | Raison |
|---|---|
| « Enable Device Flow » laissé **décoché** sur l'application | supprime le vecteur d'usurpation pour tous les utilisateurs, pas seulement pour soi |
| `--device` conservé mais avec avertissement à l'écran | une session SSH sans navigateur reste un besoin légitime |
| portées par défaut `repo read:user` | `delete_repo` est irréversible et ne s'obtient que par `--with-delete` |
| état anti-CSRF vérifié en temps constant | le rappel arrive sur un port local que tout processus de la machine peut appeler |
| serveur local à requête unique, arrêté aussitôt | aucun port laissé ouvert après la connexion |

### 7.3 Ce que PKCE ne protège pas

Le jeton, une fois obtenu, est un secret durable stocké en clair dans le `.env` de chaque utilisateur —
c'est le point faible restant, et il est indépendant du flux choisi. Trois atténuations : donner une
expiration au jeton côté GitHub, ne demander `delete_repo` qu'en cas de besoin réel, et révoquer depuis
<https://github.com/settings/applications> au moindre doute.

À noter aussi : en tant que propriétaire de l'application OAuth, on voit la liste des comptes qui l'ont
autorisée. Ce n'est pas un accès à leurs données, mais c'est une information à mentionner si l'outil est
diffusé au-delà d'un cercle restreint.
