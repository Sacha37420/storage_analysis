#!/usr/bin/env bash
# Lance l'application avec l'interpréteur enregistré dans .env.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "  .env introuvable — lancez d'abord : ./install.sh" >&2
    exit 1
fi

VENV_PYTHON="$(grep -E '^VENV_PYTHON=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"

if [ -z "${VENV_PYTHON:-}" ] || [ ! -x "$VENV_PYTHON" ]; then
    echo "  VENV_PYTHON absent ou invalide dans .env — relancez : ./install.sh --force" >&2
    exit 1
fi

# Le paquet est trouvé quel que soit le répertoire courant de l'appelant.
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec "$VENV_PYTHON" -m storage_analysis "$@"
