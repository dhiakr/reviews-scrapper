"""Make the package modules importable when running pytest from the repo root.

The modules use flat imports (``from normalizer import ...``) so the package
directory must be on sys.path. This adds it.
"""

import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)
