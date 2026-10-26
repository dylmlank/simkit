"""Test configuration.

Puts the repository root on the path so tests import `core` and `config` the
same way the servers do, and points the output directory at a temporary
location so a test run never writes into a real working directory.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    "SIMKIT_OUT", os.path.join(tempfile.gettempdir(), "simkit_tests"))
