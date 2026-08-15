"""Point d'entrée : `python -m storage_analysis`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
