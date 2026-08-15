#!/usr/bin/env bash
# Équivalent Linux/macOS de install.ps1 : détecte un Python, crée le .venv,
# installe les dépendances et enregistre les chemins dans .env (gitignoré).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
VENV_DIR="$PROJECT_ROOT/.venv"
MIN_MAJOR=3
MIN_MINOR=10

step() { printf '\n==> %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }

usable() {
    local exe="$1"
    [ -x "$exe" ] || return 1
    "$exe" -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
        >/dev/null 2>&1
}

step "1/3  Recherche d'un interpréteur Python"
BASE_PYTHON=""
BASE_SOURCE=""

# a. système
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    exe="$(command -v "$cand" 2>/dev/null || true)"
    if [ -n "$exe" ] && usable "$exe"; then BASE_PYTHON="$exe"; BASE_SOURCE="system"; break; fi
done

# b. conda
if [ -z "$BASE_PYTHON" ]; then
    for dir in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" \
               "/opt/miniconda3" "/opt/anaconda3"; do
        if usable "$dir/bin/python"; then BASE_PYTHON="$dir/bin/python"; BASE_SOURCE="conda"; break; fi
    done
fi

# c. embarqués (QGIS, Spyder, Blender...)
if [ -z "$BASE_PYTHON" ]; then
    for exe in /Applications/QGIS*.app/Contents/MacOS/bin/python3 \
               /usr/lib/qgis/python3 \
               "$HOME/.local/spyder-"*/python/bin/python3 \
               /usr/share/blender/*/python/bin/python3*; do
        if usable "$exe"; then BASE_PYTHON="$exe"; BASE_SOURCE="embedded"; break; fi
    done
fi

if [ -z "$BASE_PYTHON" ]; then
    echo "    Aucun Python >= $MIN_MAJOR.$MIN_MINOR utilisable trouvé." >&2
    exit 1
fi

BASE_PYTHON="$("$BASE_PYTHON" -c 'import sys; print(sys.executable)')"
BASE_VERSION="$("$BASE_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
info "retenu [$BASE_SOURCE] : $BASE_PYTHON ($BASE_VERSION)"

step "2/3  Environnement virtuel"
if [ "${1:-}" = "--force" ] && [ -d "$VENV_DIR" ]; then
    info "suppression du .venv existant"
    rm -rf "$VENV_DIR"
fi
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    info "création : $VENV_DIR"
    "$BASE_PYTHON" -m venv "$VENV_DIR"
fi
"$VENV_PYTHON" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt" --disable-pip-version-check
info "dépendances installées"

step "3/3  Enregistrement dans .env"
cat > "$ENV_FILE" <<ENVEOF
# Genere par install.sh - ne pas versionner, ne pas editer a la main.
# $(date '+%Y-%m-%d %H:%M:%S')

PROJECT_ROOT=$PROJECT_ROOT
BASE_PYTHON=$BASE_PYTHON
BASE_PYTHON_VERSION=$BASE_VERSION
BASE_PYTHON_SOURCE=$BASE_SOURCE
VENV_DIR=$VENV_DIR
VENV_PYTHON=$VENV_PYTHON
ENVEOF
info "$ENV_FILE"

printf '\n  Prêt.  ./run.sh scan ~ --top 20\n\n'
